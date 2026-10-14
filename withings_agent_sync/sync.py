"""Fetch and normalize Withings health data."""

from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import APIError, ConfigError
from .storage import load_json, save_json_secure

HOME = Path.home()
DEFAULT_TOKEN_FILE = HOME / ".local/state/withings-agent-sync/tokens.json"
DEFAULT_CREDS_FILE = HOME / ".config/withings-agent-sync/client.json"
# Health data lives apart from tokens so the data folder can be shared with agents.
DEFAULT_OUT_DIR = HOME / ".local/share/withings-agent-sync"
API_BASE = "https://wbsapi.withings.net"
MAX_PAGES = 1000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Withings documents these response statuses as authentication failures.
AUTH_FAILURE_STATUSES = frozenset({100, 101, 102, 200, 401})

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

ACTIVITY_FIELDS = (
    "steps,distance,elevation,soft,moderate,intense,active,calories,totalcalories,"
    "hr_average,hr_min,hr_max,hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3"
)
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


class _NoRedirectHandler(HTTPRedirectHandler):
    def _reject(self, req: Request, fp: Any, code: int, msg: str, headers: Any) -> None:
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return self._reject(req, fp, code, msg, headers)


_HTTP_OPENER = build_opener(_NoRedirectHandler())


def _safe_urlopen(request: Request, timeout: float):
    return _HTTP_OPENER.open(request, timeout=timeout)


# This name is deliberately a seam for tests and callers embedding the package.
urlopen = _safe_urlopen


def _path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name, str(default))
    if not value.strip():
        raise ConfigError(f"{name} must not be empty.")
    return Path(value).expanduser()


def default_token_file() -> Path:
    return _path_from_env("WITHINGS_TOKEN_FILE", DEFAULT_TOKEN_FILE)


def default_creds_file() -> Path:
    return _path_from_env("WITHINGS_CREDS_FILE", DEFAULT_CREDS_FILE)


def default_out_dir() -> Path:
    return _path_from_env("WITHINGS_OUT_DIR", DEFAULT_OUT_DIR)


def load_client_credentials(creds_file: Path | str | None = None) -> tuple[str | None, str | None]:
    client_id = os.getenv("WITHINGS_CLIENT_ID")
    client_secret = os.getenv("WITHINGS_CLIENT_SECRET")
    if (client_id is None) != (client_secret is None):
        raise ConfigError("Set both WITHINGS_CLIENT_ID and WITHINGS_CLIENT_SECRET, or neither.")
    if client_id is not None and client_secret is not None:
        if not client_id.strip() or not client_secret.strip():
            raise ConfigError("WITHINGS_CLIENT_ID and WITHINGS_CLIENT_SECRET must not be empty.")
        if any(ord(char) < 32 for value in (client_id, client_secret) for char in value):
            raise ConfigError("Client credentials must not contain control characters.")
        return client_id, client_secret

    path = Path(creds_file).expanduser() if creds_file is not None else default_creds_file()
    if not path.exists():
        return None, None
    data = load_json(path)
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    if not isinstance(client_id, str) or not client_id or not isinstance(client_secret, str) or not client_secret:
        raise ConfigError(f"Credentials file must contain non-empty client_id and client_secret strings: {path}")
    if not client_id.strip() or not client_secret.strip() or any(
        ord(char) < 32 for value in (client_id, client_secret) for char in value
    ):
        raise ConfigError(f"Credentials file contains empty or invalid credential strings: {path}")
    return client_id, client_secret


def post_form(path: str, data: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise APIError("Invalid API endpoint path.")
    body = urlencode({key: value for key, value in data.items() if value is not None}).encode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        if not isinstance(token, str) or any(ord(char) < 32 for char in token):
            raise ConfigError("access_token must be a non-empty string without control characters.")
        headers["Authorization"] = f"Bearer {token}"
    request = Request(API_BASE + path, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise APIError(f"Response from {path} exceeded the size limit.")
            payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except HTTPError as exc:
        exc.close()
        if 300 <= exc.code < 400:
            raise APIError(f"Refused HTTP redirect from {path}; credentials are not forwarded.") from exc
        raise APIError(f"HTTP {exc.code} from {path}. Check credentials, permissions, and API availability.") from exc
    except (HTTPException, TimeoutError, URLError, OSError) as exc:
        raise APIError(
            f"Network request to {path} failed or timed out ({_failure_kind(exc)}). "
            "Check connectivity, DNS, and TLS certificates, then retry."
        ) from exc
    except ValueError as exc:
        raise APIError(f"Invalid JSON response from {path}") from exc
    if not isinstance(payload, dict):
        raise APIError(f"Invalid response shape from {path}: expected a JSON object")
    return payload


def _failure_kind(exc: BaseException) -> str:
    # Class names identify DNS, TLS, or timeout failures without echoing untrusted reason text.
    reason = exc.reason if isinstance(exc, URLError) else None
    return type(reason if isinstance(reason, BaseException) else exc).__name__


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _require_days(days: int) -> None:
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ConfigError("days must be a positive integer")
    try:
        datetime.now(timezone.utc) - timedelta(days=days)
    except OverflowError as exc:
        raise ConfigError("days exceeds the supported date range") from exc


def _response_body(data: Any, endpoint: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise APIError(f"{endpoint} returned an invalid response")
    status = data.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        raise APIError(f"{endpoint} returned an invalid status")
    if status != 0:
        raise APIError(f"{endpoint} failed with status {status}{reauthorize_hint(status)}")
    body = data.get("body")
    if not isinstance(body, dict):
        raise APIError(f"{endpoint} returned a success response without an object body")
    return body


def reauthorize_hint(status: int) -> str:
    if status in AUTH_FAILURE_STATUSES:
        return "; Withings rejected the authorization. Run withings-agent-sync authorize again."
    return ""


def _degraded_error(data: Any) -> list[dict[str, Any]] | None:
    if (
        isinstance(data, dict)
        and isinstance(data.get("status"), int)
        and not isinstance(data["status"], bool)
        and data["status"] != 0
    ):
        return [{"error": data.get("status"), "body": data.get("body")}]
    return None


def _fetch_pages(
    path: str,
    params: dict[str, Any],
    token: str,
    endpoint: str,
    field: str,
    *,
    degrade_status: bool = False,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset: int | None = None
    seen_offsets: set[int] = {0}
    for _ in range(MAX_PAGES):
        request_params = dict(params)
        if offset is not None:
            request_params["offset"] = offset
        data = post_form(path, request_params, token)
        degraded = _degraded_error(data) if degrade_status else None
        if degraded is not None:
            return records + degraded
        body = _response_body(data, endpoint)
        page = body.get(field, [])
        if page is None:
            page = []
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise APIError(f"{endpoint} returned an invalid {field} list")
        if degrade_status and any("error" in item for item in page):
            # Only our validated numeric API-status markers may use this field;
            # provider records must not inject arbitrary text into CLI warnings.
            raise APIError(f"{endpoint} returned a record with a reserved error field")
        records.extend(page)
        more = body.get("more", False)
        if type(more) not in (bool, int) or more not in (0, 1):
            raise APIError(f"{endpoint} returned an invalid pagination flag")
        if not more:
            return records
        next_offset = body.get("offset")
        if (
            isinstance(next_offset, bool)
            or not isinstance(next_offset, int)
            or next_offset <= (offset or 0)
            or next_offset in seen_offsets
        ):
            raise APIError(f"{endpoint} returned non-progressing pagination")
        seen_offsets.add(next_offset)
        offset = next_offset
    raise APIError(f"{endpoint} exceeded the pagination limit")


def refresh_if_needed(
    tokens: dict[str, Any],
    token_file: Path | str | None = None,
    creds_file: Path | str | None = None,
) -> dict[str, Any]:
    if not isinstance(tokens, dict):
        raise ConfigError("Token file must contain a JSON object")
    for field in ("access_token", "refresh_token"):
        value = tokens.get(field)
        if value is not None and (
            not isinstance(value, str) or not value or any(ord(char) < 32 for char in value)
        ):
            raise ConfigError(f"Token {field} must be a non-empty string without control characters.")
    if tokens.get("userid") is not None and not _valid_userid(tokens["userid"]):
        raise ConfigError("Token userid must be a positive integer or a numeric string.")
    raw_expiry = tokens.get("expires_at")
    if raw_expiry is None:
        raw_expiry = 0
    if isinstance(raw_expiry, (bool, float)):
        raise ConfigError("Token expires_at must be an integer")
    try:
        expires_at = int(raw_expiry)
    except (TypeError, ValueError) as exc:
        raise ConfigError("Token expires_at must be an integer") from exc
    now = int(time.time())
    if tokens.get("access_token") and expires_at > now + 600:
        return tokens

    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConfigError("Token file has no usable refresh_token; run authorize again.")
    client_id, client_secret = load_client_credentials(creds_file)
    if not client_id or not client_secret:
        path = Path(creds_file).expanduser() if creds_file is not None else default_creds_file()
        raise ConfigError(
            "Withings access token is expired or near expiry, but client credentials are missing. "
            f"Create {path} with client_id/client_secret or set WITHINGS_CLIENT_ID/WITHINGS_CLIENT_SECRET."
        )

    response = post_form(
        "/v2/oauth2",
        {
            "action": "requesttoken",
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    body = _response_body(response, "Token refresh")
    refreshed = _make_token_payload(body)
    path = Path(token_file).expanduser() if token_file is not None else default_token_file()
    save_json_secure(path, refreshed)
    return refreshed


def _make_token_payload(
    body: Any,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise APIError("Token response body is not an object")
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise APIError("Token response did not contain access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise APIError("Token response did not contain refresh_token")
    if any(ord(char) < 32 for token in (access_token, refresh_token) for char in token):
        raise APIError("Token response contained control characters")
    raw_expiry = body.get("expires_in")
    if isinstance(raw_expiry, (bool, float)):
        raise APIError("Token response contained an invalid expires_in")
    try:
        expires_in = int(raw_expiry)
    except (TypeError, ValueError) as exc:
        raise APIError("Token response contained an invalid expires_in") from exc
    if expires_in <= 0:
        raise APIError("Token response contained a non-positive expires_in")
    userid = body.get("userid")
    if not _valid_userid(userid):
        raise APIError("Token response did not contain a valid userid")
    saved_at = int(time.time()) if now is None else now
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": saved_at + expires_in,
        "userid": userid,
        "saved_at": saved_at,
    }


def _valid_userid(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    return isinstance(value, str) and value.isascii() and value.isdigit() and any(char != "0" for char in value)


def decode_measure(measure: dict[str, Any]) -> float:
    try:
        unit = measure.get("unit", 0)
        if isinstance(unit, (bool, float)) or isinstance(measure["value"], bool):
            raise ValueError("Invalid measurement type")
        scale = 10.0 ** int(unit)
        if not scale:
            raise ValueError("Measurement unit underflow")
        result = float(measure["value"]) * scale
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise APIError("getmeas returned an invalid measurement value or unit") from exc
    if not math.isfinite(result):
        raise APIError("getmeas returned a non-finite measurement")
    return result


def fetch_measures(token: str, days: int) -> list[dict[str, Any]]:
    _require_days(days)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    groups = _fetch_pages(
        "/measure",
        {
            "action": "getmeas",
            "category": 1,
            "startdate": int(start.timestamp()),
            "enddate": int(end.timestamp()),
            "meastypes": ",".join(str(key) for key in sorted(MEASURE_TYPES)),
        },
        token,
        "getmeas",
        "measuregrps",
    )
    normalized: list[dict[str, Any]] = []
    seen_groups: set[int | str] = set()
    for group in groups:
        if group.get("category") == 2:
            continue
        group_id = group.get("grpid")
        if group_id is not None:
            if isinstance(group_id, bool) or not isinstance(group_id, (int, str)):
                raise APIError("getmeas returned an invalid group ID")
            if group_id in seen_groups:
                continue
            seen_groups.add(group_id)
        try:
            raw_date = group["date"]
            if isinstance(raw_date, (bool, float)):
                raise ValueError("Invalid timestamp")
            timestamp = int(raw_date)
            date = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
            raise APIError("getmeas returned a group without a valid date") from exc
        entry: dict[str, Any] = {
            "date": date,
            "timestamp": timestamp,
            "grpid": group.get("grpid"),
            "category": group.get("category"),
        }
        measures = group.get("measures", [])
        if measures is None:
            measures = []
        if not isinstance(measures, list):
            raise APIError("getmeas returned an invalid measures list")
        for measure in measures:
            if not isinstance(measure, dict):
                raise APIError("getmeas returned an invalid measure")
            try:
                raw_type = measure.get("type")
                if isinstance(raw_type, (bool, float)):
                    raise ValueError("Invalid measurement type")
                name = MEASURE_TYPES.get(int(raw_type))
            except (TypeError, ValueError) as exc:
                raise APIError("getmeas returned a measure with an invalid type") from exc
            if name:
                entry[name] = decode_measure(measure)
        normalized.append(entry)
    normalized.sort(key=lambda item: item["timestamp"], reverse=True)
    return normalized


def fetch_activity(token: str, days: int) -> list[dict[str, Any]]:
    _require_days(days)
    start, end = _daily_window(days)
    activities = _fetch_pages(
        "/v2/measure",
        {
            "action": "getactivity",
            "startdateymd": start,
            "enddateymd": end,
            "data_fields": ACTIVITY_FIELDS,
        },
        token,
        "getactivity",
        "activities",
        degrade_status=True,
    )
    _sort_daily_records(activities, "getactivity")
    return activities


def fetch_sleep(token: str, days: int) -> list[dict[str, Any]]:
    _require_days(days)
    start, end = _daily_window(days)
    series = _fetch_pages(
        "/v2/sleep",
        {
            "action": "getsummary",
            "startdateymd": start,
            "enddateymd": end,
            "data_fields": SLEEP_FIELDS,
        },
        token,
        "getsummary",
        "series",
        degrade_status=True,
    )
    _sort_daily_records(series, "getsummary")
    return series


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _daily_window(days: int) -> tuple[str, str]:
    # Activity and sleep dates are calendar dates, which can be ahead of UTC.
    # Request one extra day so the user's current local day is not missed.
    today = _utc_today()
    return (today - timedelta(days=days)).isoformat(), (today + timedelta(days=1)).isoformat()


def _sort_daily_records(records: list[dict[str, Any]], endpoint: str) -> None:
    for record in records:
        if "error" not in record:
            value = record.get("date")
            try:
                if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                    raise ValueError("Invalid date")
            except ValueError as exc:
                raise APIError(f"{endpoint} returned a record without a valid date") from exc
    records.sort(key=lambda item: item.get("date", ""), reverse=True)


def summarize(
    measures: list[dict[str, Any]],
    activity: list[dict[str, Any]],
    sleep: list[dict[str, Any]],
) -> dict[str, Any]:
    measures = [entry for entry in measures if entry.get("category") != 2]
    latest_measure_by_key: dict[str, Any] = {}
    for key in MEASURE_TYPES.values():
        for entry in measures:
            if key in entry:
                latest_measure_by_key[key] = {"value": entry[key], "date": entry["date"]}
                break
    valid_activity = [entry for entry in activity if "error" not in entry]
    valid_sleep = [entry for entry in sleep if "error" not in entry]
    return {
        "latest_measures": latest_measure_by_key,
        "latest_activity": valid_activity[0] if valid_activity else None,
        "latest_sleep": valid_sleep[0] if valid_sleep else None,
        "measure_count": len(measures),
        "activity_count": len(valid_activity),
        "sleep_count": len(valid_sleep),
    }


__all__ = [
    "ACTIVITY_FIELDS",
    "API_BASE",
    "DEFAULT_CREDS_FILE",
    "DEFAULT_OUT_DIR",
    "DEFAULT_TOKEN_FILE",
    "MEASURE_TYPES",
    "SLEEP_FIELDS",
    "decode_measure",
    "default_creds_file",
    "default_out_dir",
    "default_token_file",
    "fetch_activity",
    "fetch_measures",
    "fetch_sleep",
    "load_client_credentials",
    "load_json",
    "post_form",
    "refresh_if_needed",
    "save_json_secure",
    "summarize",
    "urlopen",
]
