-- OpenFlight shots in your own Supabase project.
--
-- Run this once in the project's SQL editor. Running it again is safe: it
-- creates what is missing and replaces the access rules with these ones.
--
-- What it makes, all in the public schema with a golf_ prefix so it can share
-- a project with other apps:
--   golf_devices   one row per Pi allowed to upload (holds a token hash only)
--   golf_sessions  one row per OpenFlight session
--   golf_shots     one row per shot: the headline numbers as columns, the
--                  whole shot as JSON in `data`
--
-- Who can do what:
--   * A Pi writes with the project's publishable (anon) key plus its own
--     device token in the x-openflight-device-token header. Rows are tied to
--     that device; a Pi can read and update only its own rows.
--   * Nobody can read golf_devices through the API. You see everything in the
--     dashboard's Table Editor, which is not limited by these rules.
--   * Raw radar data is never uploaded: the Pi sends shot results only.

create table if not exists public.golf_devices (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  token_sha256 text not null unique check (token_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default now()
);

-- The registered device making this API request, or null.
-- PostgREST puts the request's headers in request.headers; the token is
-- compared by its SHA-256, so the table never holds a usable secret.
create or replace function public.golf_request_device()
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  with req as (
    select nullif(current_setting('request.headers', true), '')::json
             ->> 'x-openflight-device-token' as token
  )
  select d.id
  from req
  join public.golf_devices d
    on d.token_sha256 = encode(sha256(convert_to(req.token, 'UTF8')), 'hex')
  where length(req.token) >= 32
$$;

create table if not exists public.golf_sessions (
  id uuid primary key,
  device_id uuid not null default public.golf_request_device()
    references public.golf_devices (id) on delete cascade,
  started_at timestamptz,
  ended_at timestamptz,
  info jsonb not null default '{}'::jsonb,
  uploaded_at timestamptz not null default now()
);

create table if not exists public.golf_shots (
  id uuid primary key,
  session_id uuid not null references public.golf_sessions (id) on delete cascade,
  device_id uuid not null default public.golf_request_device()
    references public.golf_devices (id) on delete cascade,
  shot_number integer,
  shot_at timestamptz,
  club text,
  profile_name text,
  ball_speed_mph real,
  club_speed_mph real,
  smash_factor real,
  launch_v_deg real,
  launch_h_deg real,
  spin_rpm real,
  spin_axis_deg real,
  carry_yards real,
  face_angle_deg real,
  club_path_deg real,
  attack_angle_deg real,
  dynamic_loft_deg real,
  data jsonb not null,
  uploaded_at timestamptz not null default now()
);

-- A Pi never sends its device id: the row takes it from the request's token.
alter table public.golf_sessions alter column device_id set default public.golf_request_device();
alter table public.golf_shots alter column device_id set default public.golf_request_device();

create index if not exists golf_shots_session_idx on public.golf_shots (session_id, shot_number);
create index if not exists golf_shots_shot_at_idx on public.golf_shots (shot_at desc);
create index if not exists golf_sessions_device_idx on public.golf_sessions (device_id, started_at desc);

revoke all on function public.golf_request_device() from public;
grant execute on function public.golf_request_device() to anon, authenticated;

alter table public.golf_devices enable row level security;
alter table public.golf_sessions enable row level security;
alter table public.golf_shots enable row level security;

revoke all on public.golf_devices from anon, authenticated;
grant select, insert, update on public.golf_sessions to anon;
grant select, insert, update on public.golf_shots to anon;

drop policy if exists golf_sessions_device_select on public.golf_sessions;
drop policy if exists golf_sessions_device_insert on public.golf_sessions;
drop policy if exists golf_sessions_device_update on public.golf_sessions;
create policy golf_sessions_device_select on public.golf_sessions
  for select to anon using (device_id = public.golf_request_device());
create policy golf_sessions_device_insert on public.golf_sessions
  for insert to anon with check (device_id = public.golf_request_device());
create policy golf_sessions_device_update on public.golf_sessions
  for update to anon
  using (device_id = public.golf_request_device())
  with check (device_id = public.golf_request_device());

drop policy if exists golf_shots_device_select on public.golf_shots;
drop policy if exists golf_shots_device_insert on public.golf_shots;
drop policy if exists golf_shots_device_update on public.golf_shots;
create policy golf_shots_device_select on public.golf_shots
  for select to anon using (device_id = public.golf_request_device());
create policy golf_shots_device_insert on public.golf_shots
  for insert to anon with check (device_id = public.golf_request_device());
create policy golf_shots_device_update on public.golf_shots
  for update to anon
  using (device_id = public.golf_request_device())
  with check (device_id = public.golf_request_device());

-- To let a Pi upload, add it with the line `openflight-cloud supabase setup`
-- prints on the Pi, for example:
--   insert into public.golf_devices (name, token_sha256)
--   values ('Garage Pi', '<64 hex characters>')
--   on conflict (token_sha256) do nothing;
