from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from withings_agent_sync import storage, sync


def write_private(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_decode_measure_units() -> None:
    assert sync.decode_measure({"value": 81271, "unit": -3}) == pytest.approx(81.271)
    assert sync.decode_measure({"value": 72, "unit": 0}) == 72.0
    assert sync.decode_measure({"value": 12, "unit": 2}) == 1200.0


def test_fetch_measures_normalizes_and_sorts_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    now = int(time.time())
    calls = []
    pages = [
        {
            "status": 0,
            "body": {
                "measuregrps": [
                    {
                        "date": now - 3600,
                        "grpid": 1,
                        "category": 1,
                        "measures": [{"type": 1, "value": 80000, "unit": -3}],
                    },
                    {
                        "date": now - 60,
                        "grpid": 2,
                        "category": 1,
                        "measures": [
                            {"type": 1, "value": 81271, "unit": -3},
                            {"type": 5, "value": 60500, "unit": -3},
                            {"type": 6, "value": 253, "unit": -1},
                            {"type": 8, "value": 20771, "unit": -3},
                            {"type": 76, "value": 57400, "unit": -3},
                            {"type": 170, "value": 7, "unit": 0},
                        ],
                    },
                ],
                "more": True,
                "offset": 11,
            },
        },
        {"status": 0, "body": {"measuregrps": [], "more": False}},
    ]

    def fake_post_form(path, data, token=None):
        calls.append((path, data, token))
        return pages.pop(0)

    monkeypatch.setattr(sync, "post_form", fake_post_form)

    result = sync.fetch_measures("access-token", 30)

    assert calls[0][0] == "/measure"
    assert "offset" not in calls[0][1]
    assert calls[1][1]["offset"] == 11
    assert calls[0][2] == "access-token"
    assert [entry["grpid"] for entry in result] == [2, 1]
    newest, oldest = result
    assert newest["weight_kg"] == pytest.approx(81.271)
    assert newest["fat_free_mass_kg"] == pytest.approx(60.5)
    assert newest["fat_ratio_pct"] == pytest.approx(25.3)
    assert newest["fat_mass_kg"] == pytest.approx(20.771)
    assert newest["muscle_mass_kg"] == pytest.approx(57.4)
    assert newest["visceral_fat_index"] == 7.0
    assert oldest["weight_kg"] == pytest.approx(80.0)


@pytest.mark.parametrize(
    ("function", "field", "path"),
    [
        (sync.fetch_activity, "activities", "/v2/measure"),
        (sync.fetch_sleep, "series", "/v2/sleep"),
    ],
)
def test_fetchers_follow_pagination(monkeypatch: pytest.MonkeyPatch, function, field, path) -> None:
    calls = []

    def fake_post_form(actual_path, data, token=None):
        calls.append((actual_path, data, token))
        offset = data.get("offset")
        if offset is None:
            records = [{"date": "2026-10-13"}]
            return {"status": 0, "body": {field: records, "more": True, "offset": 1}}
        return {"status": 0, "body": {field: [{"date": "2026-10-14"}], "more": False}}

    monkeypatch.setattr(sync, "post_form", fake_post_form)
    result = function("access-token", 30)

    assert [call[0] for call in calls] == [path, path]
    assert calls[1][1]["offset"] == 1
    assert len(result) == 2


def test_activity_and_sleep_degrade_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: {"status": 123, "body": {}})
    assert sync.fetch_activity("access-token", 30) == [{"error": 123, "body": {}}]
    assert sync.fetch_sleep("access-token", 30) == [{"error": 123, "body": {}}]


def test_pagination_rejects_non_progressing_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync,
        "post_form",
        lambda *a, **k: {"status": 0, "body": {"activities": [], "more": True, "offset": 0}},
    )
    with pytest.raises(sync.APIError, match="non-progressing"):
        sync.fetch_activity("access-token", 30)


def test_summarize_excludes_error_markers_from_counts() -> None:
    measures = [
        {"date": "new", "weight_kg": 81.0, "visceral_fat_index": 7.0},
        {"date": "old", "weight_kg": 80.0, "fat_ratio_pct": 25.0},
    ]
    activity = [{"error": 123, "body": {}}]
    sleep = [{"error": 456, "body": {}}]

    result = sync.summarize(measures, activity, sleep)

    assert result["latest_measures"]["weight_kg"] == {"value": 81.0, "date": "new"}
    assert result["latest_measures"]["fat_ratio_pct"] == {"value": 25.0, "date": "old"}
    assert result["latest_activity"] is None
    assert result["latest_sleep"] is None
    assert result["activity_count"] == 0
    assert result["sleep_count"] == 0


def test_summarize_handles_empty_inputs() -> None:
    result = sync.summarize([], [], [])
    assert result["latest_measures"] == {}
    assert result["latest_activity"] is None
    assert result["latest_sleep"] is None


def test_unexpired_token_is_not_refreshed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tokens = {"access_token": "still-valid", "refresh_token": "refresh", "expires_at": int(time.time()) + 7200}
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: pytest.fail("refresh should not call HTTP"))
    assert sync.refresh_if_needed(tokens, token_file=tmp_path / "tokens.json") is tokens


def test_refresh_only_token_is_refreshed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "tokens.json"
    tokens = {"refresh_token": "refresh", "expires_at": 1}
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "client-id")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        sync,
        "post_form",
        lambda *a, **k: {
            "status": 0,
            "body": {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 10800,
                "userid": 42,
            },
        },
    )
    result = sync.refresh_if_needed(tokens, token_file=token_file)
    assert result["access_token"] == "new-access"
    assert sync.load_json(token_file) == result
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_expired_token_without_credentials_mentions_creds_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds_file = tmp_path / "client.json"
    monkeypatch.delenv("WITHINGS_CLIENT_ID", raising=False)
    monkeypatch.delenv("WITHINGS_CLIENT_SECRET", raising=False)
    with pytest.raises(sync.ConfigError, match=str(creds_file)):
        sync.refresh_if_needed(
            {"access_token": "expired", "refresh_token": "refresh", "expires_at": 1},
            token_file=tmp_path / "tokens.json",
            creds_file=creds_file,
        )


def test_incomplete_refresh_response_does_not_replace_saved_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "tokens.json"
    previous = {"access_token": "old", "refresh_token": "old-refresh", "expires_at": 1, "userid": 42}
    storage.save_json_secure(token_file, previous)
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "client-id")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: {"status": 0, "body": {"access_token": "new"}})
    with pytest.raises(sync.APIError):
        sync.refresh_if_needed(previous, token_file=token_file)
    assert sync.load_json(token_file) == previous


def test_credentials_require_private_file(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text('{"client_id":"id","client_secret":"secret"}', encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(sync.ConfigError, match="non-private"):
        sync.load_client_credentials(path)
