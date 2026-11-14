"""Fire-and-forget push triggers for the server's shot and session paths.

Spawns ``openflight-cloud`` as a detached subprocess so an upload never blocks
or delays shot processing. The systemd timer remains the safety net that heals
wifi outages; these are just the fast path.

- At session end: ``push`` (FlightWeb and/or Supabase, whichever is active).
- After shots: ``supabase push --background``, started by the server's
  ``supabase.link.SupabaseLink``, which paces it and tracks whether the project
  is reachable. FlightWeb is left to session end: it marks a session pushed for
  good once it uploads.
"""

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

from .config import CONFIG_PATH, CloudConfig
from .supabase.config import SupabaseConfig


def _spawn_cli(
    cli_args: List[str],
    config_path: Path,
    log_dir: Path,
    popen_fn: Callable[..., Any],
) -> Any:
    """Start ``openflight-cloud <cli_args>`` detached and return ``popen_fn``'s result.

    Raises OSError or ValueError if it cannot start.
    """
    cmd = [
        sys.executable,
        "-m",
        "openflight.cloud.cli",
        "--config",
        str(config_path),
        "--log-dir",
        str(log_dir),
        *cli_args,
    ]
    return popen_fn(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def fire_push_async(
    config: CloudConfig,
    log_dir: Path,
    config_path: Path = CONFIG_PATH,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    supabase_config: Optional[SupabaseConfig] = None,
) -> bool:
    """Spawn a detached ``push`` if FlightWeb or Supabase uploads are active.

    Returns True if a push was spawned, False otherwise (inactive or spawn
    failed). The caller is on the shot/session path, so all errors are
    swallowed.
    """
    supabase_active = supabase_config is not None and supabase_config.is_active()
    if not (config.is_active() or supabase_active):
        return False
    try:
        _spawn_cli(["push"], config_path, log_dir, popen_fn)
    except (OSError, ValueError):
        return False
    return True


def start_supabase_push(
    supabase_config: SupabaseConfig,
    log_dir: Path,
    config_path: Path = CONFIG_PATH,
    popen_fn: Callable[..., Any] = subprocess.Popen,
) -> Optional[Any]:
    """Start a detached background Supabase push if it is active; the process, or None.

    Never raises. The caller may wait for the process; it runs on regardless.
    """
    if not supabase_config.is_active():
        return None
    try:
        return _spawn_cli(["supabase", "push", "--background"], config_path, log_dir, popen_fn)
    except (OSError, ValueError):
        return None
