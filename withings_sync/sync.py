"""Fetch and normalize Withings health data."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HOME = Path.home()
DEFAULT_TOKEN_FILE = HOME / ".local/state/withings-sync/tokens.json"
DEFAULT_CREDS_FILE = HOME / ".config/withings-sync/client.json"
DEFAULT_OUT_DIR = HOME / ".local/state/withings-sync"
API_BASE = "https://wbsapi.withings.net"

MEASURE_TYPES = {
    1: "weight_kg",
    4: "height_m",
    5: "fat_free_mass_kg",
    6: "fat_ratio_pct",
    8: "fat_mass_kg",
    9: "diastolic_bp_mmhg",
    10: "systolic_bp_mmhg",
    11: "heart_rate_bpm",
    12: "temperature_c",
    54: "spo2_pct",
    71: "body_temperature_c",
    73: "skin_temperature_c",
    76: "muscle_mass_kg",
    77: "hydration_kg",
    88: "bone_mass_kg",
    91: "pulse_wave_velocity_ms",
    123: "vo2_max_ml_min_kg",
    # Withings Body Scan / advanced body composition. This is an index/rating,
    # not kg or percent.
    170: "visceral_fat_index",
}

ACTIVITY_FIELDS = "steps,distance,elevation,soft,moderate,intense,active,calories,totalcalories,hr_average,hr_min,hr_max,hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3"
# Rich sleep summary fields exposed by Withings/ScanWatch 2. Unsupported fields are simply omitted by the API.
SLEEP_FIELDS = ",".join(
    [
        "breathing_disturbances_intensity",
        "chest_movement_rate_average",
        "chest_movement_rate_max",
        "chest_movement_rate_min",
        "chest_movement_rate_wellness_average",
        "chest_movement_rate_wellness_max",
        "chest_movement_rate_wellness_min",
        "deepsleepduration",
        "durationtosleep",
        "durationtowakeup",
        "hr_average",
        "hr_max",
        "hr_min",
        "lightsleepduration",
        "nb_rem_episodes",
        "out_of_bed_count",
        "remsleepduration",
        "rr_average",
        "rr_max",
        "rr_min",
        "sleep_efficiency",
        "sleep_latency",
        "sleep_score",
        "snoring",
        "snoringepisodecount",
        "total_sleep_time",
        "total_timeinbed",
        "wakeup_latency",
        "wakeupcount",
        "wakeupduration",
        "waso",
    ]
)


def _path_from_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser()


def default_token_file() -> Path:
    return _path_from_env("WITHINGS_TOKEN_FILE", DEFAULT_TOKEN_FILE)


def default_creds_file() -> Path:
    return _path_from_env("WITHINGS_CREDS_FILE", DEFAULT_CREDS_FILE)


def default_out_dir() -> Path:
    return _path_from_env("WITHINGS_OUT_DIR", DEFAULT_OUT_DIR)


def load_json(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def save_json_secure(path: Path | str, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def post_form(path: str, data: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    body = urlencode({k: v for k, v in data.items() if v is not None}).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(API_BASE + path, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {path}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"Network error from {path}: {e}") from e


def load_client_credentials(creds_file: Path | str | None = None) -> tuple[str | None, str | None]:
    cid = os.getenv("WITHINGS_CLIENT_ID")
    secret = os.getenv("WITHINGS_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    path = Path(creds_file).expanduser() if creds_file is not None else default_creds_file()
    if path.exists():
        d = load_json(path)
        return d.get("client_id"), d.get("client_secret")
    return None, None


def refresh_if_needed(
    tokens: dict[str, Any],
    token_file: Path | str | None = None,
    creds_file: Path | str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    expires_at = int(tokens.get("expires_at") or 0)
    if tokens.get("access_token") and expires_at > now + 600:
        return tokens

    client_id, client_secret = load_client_credentials(creds_file)
    if not client_id or not client_secret:
        path = Path(creds_file).expanduser() if creds_file is not None else default_creds_file()
        raise RuntimeError(
            "Withings access token is expired or near expiry, but client credentials are missing. "
            f"Create {path} with client_id/client_secret or set WITHINGS_CLIENT_ID/WITHINGS_CLIENT_SECRET."
        )

    data = post_form(
        "/v2/oauth2",
        {
            "action": "requesttoken",
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    if data.get("status") != 0:
        raise RuntimeError(f"Token refresh failed: status={data.get('status')} body={data.get('body')}")
    body = data["body"]
    body["saved_at"] = now
    body["expires_at"] = now + int(body.get("expires_in", 10800))
    path = Path(token_file).expanduser() if token_file is not None else default_token_file()
    save_json_secure(path, body)
    return body


def decode_measure(measure: dict[str, Any]) -> float:
    return float(measure["value"]) * (10 ** int(measure.get("unit", 0)))


def fetch_measures(token: str, days: int) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    data = post_form(
        "/measure",
        {
            "action": "getmeas",
            "startdate": int(start.timestamp()),
            "enddate": int(end.timestamp()),
            "meastypes": ",".join(str(k) for k in sorted(MEASURE_TYPES)),
        },
        token,
    )
    if data.get("status") != 0:
        raise RuntimeError(f"getmeas failed: status={data.get('status')} body={data.get('body')}")
    groups = data.get("body", {}).get("measuregrps", []) or []
    normalized = []
    for g in groups:
        entry: dict[str, Any] = {
            "date": datetime.fromtimestamp(int(g.get("date", 0)), tz=timezone.utc).isoformat(),
            "timestamp": int(g.get("date", 0)),
            "grpid": g.get("grpid"),
            "category": g.get("category"),
        }
        for m in g.get("measures", []) or []:
            name = MEASURE_TYPES.get(int(m.get("type")))
            if name:
                entry[name] = decode_measure(m)
        normalized.append(entry)
    normalized.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return normalized


def fetch_activity(token: str, days: int) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    data = post_form(
        "/v2/measure",
        {
            "action": "getactivity",
            "startdateymd": start.isoformat(),
            "enddateymd": end.isoformat(),
            "data_fields": ACTIVITY_FIELDS,
        },
        token,
    )
    if data.get("status") != 0:
        # Some accounts/scopes/devices won't have activity; keep sync useful.
        return [{"error": data.get("status"), "body": data.get("body")}]
    activities = data.get("body", {}).get("activities", []) or []
    activities.sort(key=lambda x: x.get("date", ""), reverse=True)
    return activities


def fetch_sleep(token: str, days: int) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    data = post_form(
        "/v2/sleep",
        {
            "action": "getsummary",
            "startdateymd": start.isoformat(),
            "enddateymd": end.isoformat(),
            "data_fields": SLEEP_FIELDS,
        },
        token,
    )
    if data.get("status") != 0:
        return [{"error": data.get("status"), "body": data.get("body")}]
    series = data.get("body", {}).get("series", []) or []
    series.sort(key=lambda x: x.get("date", ""), reverse=True)
    return series


def summarize(
    measures: list[dict[str, Any]],
    activity: list[dict[str, Any]],
    sleep: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_measure_by_key: dict[str, Any] = {}
    for key in MEASURE_TYPES.values():
        for entry in measures:
            if key in entry:
                latest_measure_by_key[key] = {"value": entry[key], "date": entry["date"]}
                break
    return {
        "latest_measures": latest_measure_by_key,
        "latest_activity": activity[0] if activity else None,
        "latest_sleep": sleep[0] if sleep else None,
        "measure_count": len(measures),
        "activity_count": len(activity),
        "sleep_count": len(sleep),
    }
