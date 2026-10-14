"""Command-line interface for withings-sync."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import __version__
from . import auth, sync


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="withings-sync",
        description="Sync Withings body composition, activity, and sleep data to local JSON.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    authorize = subparsers.add_parser("authorize", help="authorize this app with Withings")
    authorize.add_argument("--port", type=int, default=8383, help="localhost callback port (default: 8383)")

    sync_parser = subparsers.add_parser("sync", help="fetch and save recent Withings data")
    sync_parser.add_argument(
        "--days",
        type=int,
        default=int(os.getenv("WITHINGS_SYNC_DAYS", "30")),
        help="number of days to fetch (default: WITHINGS_SYNC_DAYS or 30)",
    )
    sync_parser.add_argument("--out", type=Path, help="output directory (default: WITHINGS_OUT_DIR)")
    sync_parser.add_argument("--history", type=Path, help="history JSONL path (default: <out>/withings_sync_history.jsonl)")
    sync_parser.add_argument("--quiet", action="store_true", help="do not print the successful sync summary")
    return parser


def _run_sync(args: argparse.Namespace) -> int:
    token_file = sync.default_token_file()
    out_dir = args.out.expanduser() if args.out else sync.default_out_dir()
    history_file = args.history.expanduser() if args.history else out_dir / "withings_sync_history.jsonl"

    if not token_file.exists():
        print(f"Missing token file: {token_file}", file=sys.stderr)
        return 2
    try:
        tokens = sync.load_json(token_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read token file {token_file}: {exc}", file=sys.stderr)
        return 2
    if not tokens.get("access_token"):
        print("Token file has no access_token", file=sys.stderr)
        return 2

    try:
        tokens = sync.refresh_if_needed(tokens, token_file=token_file)
        token = tokens.get("access_token")
        if not token:
            print("Token file has no access_token", file=sys.stderr)
            return 2
        measures = sync.fetch_measures(token, args.days)
        activity = sync.fetch_activity(token, args.days)
        sleep = sync.fetch_sleep(token, args.days)
        payload = {
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "days": args.days,
            "userid": tokens.get("userid"),
            "measures": measures,
            "activity": activity,
            "sleep": sleep,
            "summary": sync.summarize(measures, activity, sleep),
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "withings_latest.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with history_file.open("a") as f:
            f.write(json.dumps({"synced_at": payload["synced_at"], "summary": payload["summary"]}, sort_keys=True) + "\n")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        if "client credentials are missing" in str(exc):
            return 2
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1

    summary = payload["summary"]
    if not args.quiet:
        print(
            f"Withings sync ok: measures={summary['measure_count']} "
            f"activity={summary['activity_count']} sleep={summary['sleep_count']}"
        )
        for key in ("weight_kg", "fat_ratio_pct", "fat_free_mass_kg", "muscle_mass_kg", "visceral_fat_index"):
            item = summary["latest_measures"].get(key)
            if item:
                print(f"{key}: {item['value']} at {item['date']}")
        print(f"Wrote {out_dir / 'withings_latest.json'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "authorize":
            auth.run_authorize(port=args.port)
            return 0
        if args.command == "sync":
            return _run_sync(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
