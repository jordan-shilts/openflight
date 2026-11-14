"""``openflight-cloud supabase`` commands: setup, test, status, on/off.

Each takes its dependencies explicitly (config, paths, client, input and
output) so it is testable without the network or a terminal. Uploading itself
is ``push.push``.
"""

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import push
from .client import Outcome, SupabaseClient
from .config import (
    SupabaseConfig,
    check_publishable_key,
    generate_device_token,
    normalize_project_url,
    save_supabase_config,
    token_sha256,
    uses_bearer,
)

OutFn = Callable[[str], None]

DEFAULT_DEVICE_NAME = "openflight pi"
MAX_DEVICE_NAME_CHARS = 64
SKIP_ANSWERS = frozenset({"skip", "s", "n", "no"})


def make_client(config: SupabaseConfig) -> SupabaseClient:
    """A client for the configured project."""
    return SupabaseClient(config.supabase_url, config.supabase_key, config.supabase_device_token)


def clean_device_name(raw: Optional[str]) -> str:
    """One printable line of at most 64 characters, so the SQL stays one line."""
    printable = "".join(ch if ch.isprintable() else " " for ch in (raw or ""))
    name = " ".join(printable.split())[:MAX_DEVICE_NAME_CHARS].strip()
    return name or DEFAULT_DEVICE_NAME


def registration_sql(device_name: str, token_hash: str) -> str:
    """The one line the owner runs in the SQL editor to let this Pi upload."""
    name = device_name.replace("'", "''")
    return (
        "insert into public.golf_devices (name, token_sha256) "
        f"values ('{name}', '{token_hash}') on conflict (token_sha256) do nothing;"
    )


def describe_key(key: str) -> str:
    """How the key is sent, without the parts of a legacy JWT that vary."""
    if uses_bearer(key):
        return "legacy anon JWT (sent as apikey and Authorization: Bearer)"
    return f"publishable ({key[:24]}...)"


@dataclass
class SetupOptions:
    """What ``supabase setup`` was given on the command line."""

    url: Optional[str] = None
    key: Optional[str] = None
    name: Optional[str] = None
    new_token: bool = False
    # None: ask, when there is a terminal to ask on.
    test: Optional[bool] = None


@dataclass
class Console:
    """Where setup reads answers and writes output."""

    out: OutFn = print
    input_fn: Callable[[str], str] = input
    interactive: bool = False


def _value(given: Optional[str], validate, prompt: str, console: Console) -> Optional[str]:
    """``given`` if valid, else ask until valid; None if it cannot be had."""
    if given is not None:
        try:
            return validate(given)
        except ValueError as exc:
            console.out(f"Error: {exc}")
            return None
    while True:
        try:
            answer = console.input_fn(prompt)
        except EOFError:
            return None
        try:
            return validate(answer)
        except ValueError as exc:
            console.out(f"  {exc}")


def _wants_test(options: SetupOptions, console: Console) -> bool:
    if options.test is not None:
        return options.test
    if not console.interactive:
        return False
    try:
        answer = console.input_fn(
            "Run that line in the SQL editor, then press Enter to test the connection "
            "(or type skip): "
        )
    except EOFError:
        return False
    return answer.strip().lower() not in SKIP_ANSWERS


def cmd_setup(
    existing: Optional[SupabaseConfig],
    config_path: Path,
    log_dir: Path,
    options: SetupOptions,
    console: Console,
    *,
    client_factory: Callable[[SupabaseConfig], Any] = make_client,
) -> bool:
    """Save the project URL, key and a device token; print the SQL that registers it.

    Re-running keeps the existing token (so the Pi stays the same device)
    unless ``options.new_token``. Returns False if nothing was saved or the
    connection test failed.
    """
    out = console.out
    current = existing or SupabaseConfig()
    url = _value(options.url, normalize_project_url, "Supabase project URL: ", console)
    key = (
        _value(
            options.key, check_publishable_key, "Publishable key (sb_publishable_...): ", console
        )
        if url
        else None
    )
    if not (url and key):
        out("Setup stopped; nothing saved.")
        return False

    token = current.supabase_device_token
    keep_token = bool(token) and not options.new_token
    if not keep_token:
        token = generate_device_token()
    config = SupabaseConfig(
        supabase_url=url,
        supabase_key=key,
        supabase_device_token=token,
        supabase_device_name=clean_device_name(
            options.name or current.supabase_device_name or socket.gethostname()
        ),
        enabled=True,
    )
    save_supabase_config(config, config_path)

    out(f"Saved {config_path} (readable only by you; the device token never leaves this Pi).")
    if keep_token:
        out("Kept this Pi's device token (--new-token makes a new one).")
    out("")
    out("Register this Pi: run this line in the project's SQL editor.")
    out("")
    out(registration_sql(config.supabase_device_name, token_sha256(token)))
    out("")
    if _wants_test(options, console):
        return cmd_test(config, log_dir, client_factory(config), out=out, show_sql=False)
    out("Then check the connection with: openflight-cloud supabase test")
    return True


def cmd_test(
    config: SupabaseConfig, log_dir: Path, client, out: OutFn = print, show_sql: bool = True
) -> bool:
    """Check the URL, key, schema and device registration. Writes no rows.

    Returns True when this Pi is registered; also lifts a pause from an earlier
    refusal so background uploads resume at once. ``show_sql`` repeats the
    registration line for an unregistered Pi (setup has just printed it).
    """
    if not config.is_configured():
        out("Supabase is not set up. Run `openflight-cloud supabase setup`.")
        return False
    out(f"Testing {config.supabase_url} (this writes nothing)...")
    reply = client.registered_device()
    if reply.outcome is Outcome.OK and reply.body:
        push.clear_pause(log_dir)
        state = (
            "uploads are on" if config.enabled else "uploads are off (openflight-cloud supabase on)"
        )
        out(f"Connected. This Pi is registered as device {reply.body}; {state}.")
        return True
    if reply.outcome is Outcome.OK and show_sql:
        out("Connected, but this Pi is not registered yet. Run this line in the SQL editor:")
        out("")
        name = config.supabase_device_name or DEFAULT_DEVICE_NAME
        out(registration_sql(name, token_sha256(config.supabase_device_token)))
        out("")
        out("Then run `openflight-cloud supabase test` again.")
    elif reply.outcome is Outcome.OK:
        out(
            "Connected, but this Pi is not registered yet. Run the line above in the SQL "
            "editor, then `openflight-cloud supabase test`."
        )
    elif reply.outcome is Outcome.UNAUTHORIZED:
        out(
            f"The project refused the key ({reply.message}). Copy the publishable key from "
            "Project Settings > API Keys and re-run `openflight-cloud supabase setup`."
        )
    elif reply.outcome is Outcome.SCHEMA_MISSING:
        out(
            f"The OpenFlight tables are not in this project ({reply.message}). Run "
            "scripts/supabase/schema.sql in the SQL editor, then test again."
        )
    elif reply.outcome is Outcome.RETRY:
        out(f"Could not reach {config.supabase_url} ({reply.message}).")
    else:
        out(f"Unexpected answer from {config.supabase_url} ({reply.message}).")
    return False


def cmd_set_enabled(
    config: Optional[SupabaseConfig], config_path: Path, enabled: bool, out: OutFn = print
) -> bool:
    """``supabase on`` / ``supabase off``."""
    if config is None or not config.is_configured():
        out("Supabase is not set up. Run `openflight-cloud supabase setup`.")
        return False
    config.enabled = enabled
    save_supabase_config(config, config_path)
    if enabled:
        out("Supabase uploads on: shots upload within seconds while you play.")
    else:
        out("Supabase uploads off: nothing is sent until `openflight-cloud supabase on`.")
    return True


def cmd_status(
    config: Optional[SupabaseConfig],
    log_dir: Path,
    out: OutFn = print,
    now_fn: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Report config, per-session progress, the last upload and any pause. Offline."""
    if config is None or not config.is_configured():
        out("Supabase:   not set up. Run `openflight-cloud supabase setup`.")
        return {"configured": False}
    out(f"Supabase:   {'on' if config.enabled else 'off (openflight-cloud supabase on resumes)'}")
    out(f"Project:    {config.supabase_url}")
    out(f"Key:        {describe_key(config.supabase_key)}")
    token_hash = token_sha256(config.supabase_device_token)
    out(f"Device:     {config.supabase_device_name or '-'} (token sha256 {token_hash[:12]}...)")

    counts = push.session_overview(log_dir)
    out(
        f"Sessions:   {counts['total']} total | {counts['up_to_date']} up to date | "
        f"{counts['waiting']} with new data | {counts['rejected']} refused"
    )
    out(f"Shots sent: {counts['shots_sent']}")
    pending = push.pending_shot_count(log_dir)
    out(f"Waiting:    {pending} shot(s) not uploaded yet")
    state = push.load_state(log_dir)
    if state.get("last_upload_at"):
        out(f"Last sent:  {state['last_upload_at']} ({state.get('last_upload_shots', 0)} shot(s))")
    link = state.get("link")
    if isinstance(link, dict) and isinstance(link.get("at"), (int, float)):
        detail = f" ({link['message']})" if link.get("message") else ""
        out(f"Last check: {link.get('state')} at {push.format_time(link['at'])}{detail}")
    pause = push.active_pause(log_dir, config, now_fn())
    if pause is not None:
        out(
            f"Paused:     {pause.get('message')}. Background uploads retry after "
            f"{push.format_time(pause['until'])}; `openflight-cloud supabase test` checks now."
        )
    elif state.get("last_error"):
        out(f"Last error: {state['last_error']}")
    rejected = push.rejected_sessions(log_dir)
    for item in rejected:
        out(f"Refused:    {item['file']}: {item.get('message')}")
    return {
        "configured": True,
        "enabled": config.enabled,
        "counts": counts,
        "pending": pending,
        "paused": pause is not None,
        "rejected": [item["file"] for item in rejected],
    }
