"""Command-line interface for withings-agent-sync."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from . import __version__
from . import auth, sync
from .errors import ConfigError
from .storage import append_jsonl_secure, check_history, exclusive_locks, validate_paths

SNAPSHOT_NAME = "withings_latest.json"
HISTORY_NAME = "withings_sync_history.jsonl"


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _days(value: str) -> int:
    result = _positive_int(value)
    try:
        datetime.now(timezone.utc) - timedelta(days=result)
    except OverflowError as exc:
        raise argparse.ArgumentTypeError("exceeds the supported date range") from exc
    return result


def _port(value: str) -> int:
    result = _positive_int(value)
    if result > 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="withings-agent-sync",
        description="Sync Withings body composition, activity, and sleep data to local JSON.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    authorize = subparsers.add_parser("authorize", help="authorize this app with Withings")
    authorize.add_argument("--port", type=_port, default=8383, help="localhost callback port (default: 8383)")
    authorize.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")

    sync_parser = subparsers.add_parser("sync", help="fetch and save recent Withings data")
    sync_parser.add_argument("--days", type=_days, help="number of days to fetch (WITHINGS_SYNC_DAYS or 30)")
    sync_parser.add_argument("--out", type=Path, help="output directory (default: WITHINGS_OUT_DIR)")
    sync_parser.add_argument("--history", type=Path, help="history JSONL path (default: <out>/withings_sync_history.jsonl)")
    sync_parser.add_argument("--quiet", action="store_true", help="do not print the successful sync summary")
    return parser


def _configured_days(value: int | None) -> int:
    if value is not None:
        return value
    raw = os.getenv("WITHINGS_SYNC_DAYS", "30")
    try:
        return _days(raw)
    except argparse.ArgumentTypeError as exc:
        raise ConfigError(f"WITHINGS_SYNC_DAYS {exc}") from exc


def _run_sync(args: argparse.Namespace) -> int:
    days = _configured_days(args.days)
    token_file = sync.default_token_file()
    out_dir = args.out.expanduser() if args.out else sync.default_out_dir()
    history_file = args.history.expanduser() if args.history else out_dir / HISTORY_NAME
    latest_file = out_dir / SNAPSHOT_NAME
    validate_paths(
        {
            "token": token_file,
            "credentials": sync.default_creds_file(),
            "snapshot": latest_file,
            "history": history_file,
        },
        locked=("token", "snapshot", "history"),
    )
    if not token_file.exists():
        raise ConfigError(f"Missing token file: {token_file}. Run withings-agent-sync authorize first.")

    with exclusive_locks([token_file, latest_file, history_file]):
        check_history(history_file)
        tokens = sync.load_json(token_file)
        tokens = sync.refresh_if_needed(tokens, token_file=token_file)
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token:
            raise ConfigError("Token refresh produced no access_token; run authorize again.")

        measures = sync.fetch_measures(token, days)
        activity = sync.fetch_activity(token, days)
        sleep = sync.fetch_sleep(token, days)
        partial = any("error" in entry for entry in activity + sleep)
        payload = {
            "schema_version": 1,
            "status": "partial" if partial else "ok",
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "days": days,
            "userid": tokens.get("userid"),
            "measures": measures,
            "activity": activity,
            "sleep": sleep,
            "summary": sync.summarize(measures, activity, sleep),
        }
        sync.save_json_secure(latest_file, payload)
        append_jsonl_secure(
            history_file,
            {key: payload[key] for key in ("schema_version", "status", "synced_at", "summary")},
        )

    summary = payload["summary"]
    for name, entries in (("activity", activity), ("sleep", sleep)):
        for entry in entries:
            status = entry.get("error")
            if isinstance(status, int) and not isinstance(status, bool):
                print(f"Warning: {name} is incomplete (Withings status {status}).", file=sys.stderr)
    if not args.quiet:
        print(
            f"Withings sync {payload['status']}: measures={summary['measure_count']} "
            f"activity={summary['activity_count']} sleep={summary['sleep_count']}"
        )
        for key in ("weight_kg", "fat_ratio_pct", "fat_free_mass_kg", "muscle_mass_kg", "visceral_fat_index"):
            item = summary["latest_measures"].get(key)
            if item:
                print(f"{key}: {item['value']} at {item['date']}")
        print(f"Wrote {latest_file}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "authorize":
            auth.run_authorize(port=args.port, open_browser=not args.no_browser)
            return 0
        if args.command == "sync":
            return _run_sync(args)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError, OverflowError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
