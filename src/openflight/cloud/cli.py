"""``openflight-cloud`` command-line entry point.

openflight-cloud link              # one-time device pairing
openflight-cloud push [--dry-run]  # filter + upload anything unpushed (FlightWeb and Supabase)
openflight-cloud status            # linked? queued? parked? last error?
openflight-cloud raw on|off        # opt in/out of raw radar + L3 dump uploads

openflight-cloud supabase setup    # point this Pi at your own Supabase project
openflight-cloud supabase test     # check the key, schema and registration
openflight-cloud supabase status   # what has gone up, what is waiting
openflight-cloud supabase push     # upload new shots now
openflight-cloud supabase on|off   # resume / pause Supabase uploads
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from ..session_logger import SessionLogger
from . import commands
from .client import CloudClient
from .config import CONFIG_PATH, CloudConfig, load_config, save_config
from .supabase import commands as supabase_commands, push as supabase_push
from .supabase.config import SupabaseConfig, config_path_for, load_supabase_config

DEFAULT_LOG_DIR = SessionLogger.DEFAULT_LOG_DIR


def _build_parser() -> argparse.ArgumentParser:
    # Shared options usable either before or after the subcommand. Defaults
    # are suppressed here and supplied by main(), because argparse copies a
    # subparser's defaults over the parent namespace, which would discard a
    # value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"Path to cloud config (default: {CONFIG_PATH}).",
    )
    common.add_argument(
        "--log-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"Session log directory (default: {DEFAULT_LOG_DIR}).",
    )

    parser = argparse.ArgumentParser(
        prog="openflight-cloud",
        description=(
            "Push filtered OpenFlight session logs to the FlightWeb cloud and/or "
            "your own Supabase project."
        ),
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")

    link = sub.add_parser(
        "link", parents=[common], help="Pair this device with a FlightWeb account."
    )
    link.add_argument("--device-name", default=None, help="Device label (default: hostname).")

    push = sub.add_parser("push", parents=[common], help="Upload any unpushed sessions.")
    push.add_argument(
        "--dry-run",
        action="store_true",
        help="Show exactly which entries would upload; send nothing.",
    )
    push.add_argument(
        "--retry",
        nargs="?",
        const="__ALL__",
        default=None,
        metavar="SESSION",
        help=(
            "Re-upload sessions that were parked or failed. Give a session "
            "filename (or substring) to force re-upload a specific one even if "
            "it was already sent."
        ),
    )

    sub.add_parser("status", parents=[common], help="Show link state, queue, and parked sessions.")

    raw = sub.add_parser(
        "raw",
        parents=[common],
        help="Opt in/out of uploading raw radar data and IWR6843 L3 dumps (for testing).",
    )
    raw.add_argument("state", choices=["on", "off"])

    _add_supabase_parser(sub, common)
    return parser


def _add_supabase_parser(sub, common: argparse.ArgumentParser) -> None:
    supabase = sub.add_parser(
        "supabase",
        parents=[common],
        help="Upload shots to your own Supabase project.",
        description=(
            "Upload shots to your own Supabase project. The config lives beside "
            "--config as supabase.json."
        ),
    )
    supabase_sub = supabase.add_subparsers(dest="supabase_command")

    setup = supabase_sub.add_parser(
        "setup",
        parents=[common],
        help="Save the project URL and key, make a device token, print the SQL to register it.",
    )
    setup.add_argument("--url", help="Project URL, https://<ref>.supabase.co (prompted if absent).")
    setup.add_argument("--key", help="Publishable key, sb_publishable_... (prompted if absent).")
    setup.add_argument("--name", help="Device name in golf_devices (default: hostname).")
    setup.add_argument(
        "--new-token",
        action="store_true",
        help="Make a new device token even if this Pi has one (it must be registered again).",
    )
    test_choice = setup.add_mutually_exclusive_group()
    test_choice.add_argument(
        "--test",
        dest="run_test",
        action="store_const",
        const=True,
        default=None,
        help="Test the connection right after setup.",
    )
    test_choice.add_argument(
        "--no-test",
        dest="run_test",
        action="store_const",
        const=False,
        help="Do not offer the connection test.",
    )

    supabase_sub.add_parser(
        "test", parents=[common], help="Check the key, schema and registration (writes nothing)."
    )
    supabase_sub.add_parser(
        "status", parents=[common], help="Show what has been uploaded and what is waiting."
    )
    supabase_push_parser = supabase_sub.add_parser(
        "push", parents=[common], help="Upload new shots now."
    )
    supabase_push_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be sent; send nothing."
    )
    supabase_push_parser.add_argument(
        "--resend",
        action="store_true",
        help="Forget what was sent and send every session again (safe: rows are upserted).",
    )
    supabase_push_parser.add_argument(
        "--background",
        action="store_true",
        help="Skip while paused after the project refused this Pi (used by the server).",
    )
    supabase_sub.add_parser("on", parents=[common], help="Resume Supabase uploads.")
    supabase_sub.add_parser("off", parents=[common], help="Pause Supabase uploads.")


def _load_supabase(cloud_config_path: Path) -> Optional[SupabaseConfig]:
    """The Supabase config beside ``cloud_config_path``; None if absent or unreadable."""
    path = config_path_for(cloud_config_path)
    try:
        return load_supabase_config(path)
    except (OSError, ValueError) as exc:
        print(f"Supabase: could not read {path} ({exc}); ignoring it. Re-run setup to replace it.")
        return None


def _supabase_main(args: argparse.Namespace) -> int:
    path = config_path_for(args.config)
    config = _load_supabase(args.config)
    command = args.supabase_command

    if command == "setup":
        options = supabase_commands.SetupOptions(
            url=args.url,
            key=args.key,
            name=args.name,
            new_token=args.new_token,
            test=args.run_test,
        )
        console = supabase_commands.Console(interactive=sys.stdin.isatty())
        ok = supabase_commands.cmd_setup(config, path, args.log_dir, options, console)
        return 0 if ok else 1
    if command == "status":
        supabase_commands.cmd_status(config, args.log_dir)
        return 0
    if command in ("on", "off"):
        ok = supabase_commands.cmd_set_enabled(config, path, command == "on")
        return 0 if ok else 1

    config = config or SupabaseConfig()
    client = supabase_commands.make_client(config)
    if command == "test":
        return 0 if supabase_commands.cmd_test(config, args.log_dir, client) else 1
    if command == "push":
        summary = supabase_push.push(
            config,
            args.log_dir,
            client,
            dry_run=args.dry_run,
            resend=args.resend,
            respect_pause=args.background,
        )
        return 1 if summary.needs_owner else 0
    print("usage: openflight-cloud supabase {setup,test,status,push,on,off}")
    return 1


def _push_all(args: argparse.Namespace, config: CloudConfig) -> int:
    """``push``: FlightWeb if linked, then Supabase if set up.

    With neither configured, FlightWeb's push still runs so its "inactive"
    message tells the user how to start.
    """
    supabase_config = _load_supabase(args.config)
    supabase_active = supabase_config is not None and supabase_config.is_active()
    rc = 0
    if config.is_active() or not supabase_active:
        retry = args.retry is not None
        session = None if args.retry == "__ALL__" else args.retry
        summary = commands.cmd_push(
            config,
            args.log_dir,
            _client(config),
            dry_run=args.dry_run,
            retry=retry,
            session=session,
        )
        if summary.get("needs_relink"):
            rc = 1
    if supabase_active:
        result = supabase_push.push(
            supabase_config,
            args.log_dir,
            supabase_commands.make_client(supabase_config),
            dry_run=args.dry_run,
            respect_pause=True,
        )
        if result.needs_owner:
            rc = 1
    return rc


def _client(config: CloudConfig) -> CloudClient:
    """Build a CloudClient from persisted config."""
    return CloudClient(config.endpoint, token=config.device_token or None)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    defaults = argparse.Namespace(config=CONFIG_PATH, log_dir=DEFAULT_LOG_DIR)
    args = parser.parse_args(argv, namespace=defaults)

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "supabase":
        return _supabase_main(args)

    config = load_config(args.config) or CloudConfig()

    if args.command == "link":
        ok = commands.cmd_link(config, args.config, _client(config), device_name=args.device_name)
        return 0 if ok else 1

    if args.command == "push":
        return _push_all(args, config)

    if args.command == "status":
        commands.cmd_status(config, args.log_dir, _client(config))
        return 0

    if args.command == "raw":
        config.upload_raw = args.state == "on"
        save_config(config, args.config)
        if config.upload_raw:
            print(
                "Raw uploads ON: all unpushed sessions upload unfiltered (raw radar data included) "
                "and each shot's IWR6843 L3 dump uploads too. The OpenFlight server saves "
                "L3 dumps to disk while this is on (restart it to apply)."
            )
        else:
            print("Raw uploads OFF: sessions upload filtered shot summaries only.")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
