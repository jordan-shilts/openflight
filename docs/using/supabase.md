---
icon: lucide/database
---

# Upload Shots to Your Own Supabase

OpenFlight can put every shot in a [Supabase](https://supabase.com) project you
own, so you can query, chart or share your history with your own tools. It
works alongside [FlightWeb cloud sync](cloud-sync.md) or instead of it: set up
either, both or neither.

Shots appear within seconds while you play. The Pi uploads the same filtered
results FlightWeb gets: raw radar data never leaves the Pi.

## Set it up

You need the project's URL and its **publishable** key, both under
**Project Settings > API Keys** (the URL looks like
`https://abcdefghijklmnopqrst.supabase.co`). Never put a secret or
`service_role` key on the Pi: setup refuses them.

1. **Create the tables.** In the Supabase dashboard open **SQL Editor**, paste
   the whole of
   [`scripts/supabase/schema.sql`](https://github.com/open-flight/openflight/blob/main/scripts/supabase/schema.sql)
   and run it. It creates `golf_devices`, `golf_sessions` and `golf_shots` and
   their access rules. Running it again is safe.
2. **Set up the Pi.** On the Pi run:

    ```bash
    openflight-cloud supabase setup
    ```

    It asks for the URL and key (or pass `--url` and `--key`), makes this Pi a
    device token, saves everything to `~/.config/openflight/supabase.json`
    (readable only by you), and prints one SQL line:

    ```text
    Supabase project URL: https://abcdefghijklmnopqrst.supabase.co
    Publishable key (sb_publishable_...): sb_publishable_Xq3vR8kLmN2pT5wY7zA1bC_dE4fG6hJ
    Saved /home/pi/.config/openflight/supabase.json (readable only by you; the device token never leaves this Pi).

    Register this Pi: run this line in the project's SQL editor.

    insert into public.golf_devices (name, token_sha256) values ('garage-pi', 'c97da8a914b4c4eb80aff79d6744d0c4afa9652a25051316161eb004eea54b7c') on conflict (token_sha256) do nothing;

    Run that line in the SQL editor, then press Enter to test the connection (or type skip):
    ```

    The device name defaults to the Pi's hostname (`--name` changes it).

3. **Register the Pi.** Paste that line into the SQL editor and run it. It
   stores only a hash of the token, so the table never holds anything a
   stranger could upload with.
4. **Test.** Back on the Pi press Enter, or run
   `openflight-cloud supabase test` later. It writes nothing:

    ```text
    Testing https://abcdefghijklmnopqrst.supabase.co (this writes nothing)...
    Connected. This Pi is registered as device 5b0f3c1e-2d4a-4e6b-8c9d-0e1f2a3b4c5d; uploads are on.
    ```

5. **Hit a shot**, then open **Table Editor > golf_shots** in the dashboard.

The first upload also sends every session already on the Pi.

## What gets uploaded

| Table | One row per | Columns |
|---|---|---|
| `golf_sessions` | session | `started_at`, `ended_at`, and `info`: the session's start and end entries |
| `golf_shots` | shot | `club`, `ball_speed_mph`, `club_speed_mph`, `smash_factor`, `launch_v_deg`, `launch_h_deg`, `spin_rpm`, `spin_axis_deg`, `carry_yards`, `face_angle_deg`, `club_path_deg`, `attack_angle_deg`, `dynamic_loft_deg`, and the whole shot as JSON in `data` |

A value the Pi did not have is `null`. Club path and attack angle are the club
radar's, or the camera's at confidence high or medium, the same ones that
drive the ball flight. Times carry the Pi's UTC offset.

Each Pi can read and change only its own rows. You see all of them in the
dashboard, which those rules do not limit.

## Online and offline

The server always knows whether it can reach your project, and the kiosk
says so: open the menu (the footer logo) and look under **System > Cloud**.

| Cloud says | Meaning | What the Pi does |
|---|---|---|
| Online · all shots saved | The last upload or check got an answer. | Each shot goes up within seconds: one background upload at most every 10 seconds, so a quick run of shots goes up together. |
| Offline · 12 shots waiting, will send when back online | No answer: no internet (for example the Pi on its own hotspot), DNS, a timeout, or Supabase itself failing (5xx). | Shots wait in the session logs on the Pi. It checks the project every 30 seconds with one small request; the moment it answers, it sends the backlog, oldest first, then goes back to per-shot uploads. |
| Device not registered or wrong key | The project refused this Pi (or *Supabase tables missing* when `schema.sql` was not run). | Nothing is sent until you fix it. `openflight-cloud supabase test` then resumes at once; otherwise it tries again after 30 minutes. |
| Not set up | No Supabase config, uploads switched off, or session logging off. | Nothing. |

The same is available as `GET /api/cloud/status` on the Pi, for example
`{"state": "offline", "pending": 12, "message": "network error: ...", "reason": ""}`,
and as a `cloud_status` Socket.IO event whenever it changes.

A shot only counts as uploaded once Supabase has accepted it. Each upload
reads only what was logged since the last one (a sidecar
`<session>.jsonl.supabase` remembers where it got to), and rows are upserted by
an id derived from the session and shot number. So a connection that drops
mid-upload, a crash or a power cut never loses a shot or makes a duplicate: the
next upload sends that batch again.

Besides the server's own uploads, `openflight-cloud push` at session end and
every ~10 minutes from the `openflight-cloud.timer` (see
[Cloud sync](cloud-sync.md#systemd-units)) covers Supabase too, and
`openflight-cloud supabase push` uploads by hand. A session Supabase refuses
(any other 4xx) is skipped and listed by `status`; the rest carry on.

## Commands

```bash
openflight-cloud supabase setup            # URL, key, device token, SQL line
openflight-cloud supabase test             # key, tables and registration; writes nothing
openflight-cloud supabase status           # what went up, what is waiting, any pause
openflight-cloud supabase push             # upload now
openflight-cloud supabase push --dry-run   # show what would be sent
openflight-cloud supabase push --resend    # send every session again (safe)
openflight-cloud supabase off              # pause uploads; `on` resumes
```

Re-running `setup` keeps the Pi's token, so it stays registered; add
`--new-token` to replace it (then register the new line). To stop for good,
delete `~/.config/openflight/supabase.json` and the Pi's row in
`golf_devices` (deleting it also deletes that Pi's sessions and shots).
