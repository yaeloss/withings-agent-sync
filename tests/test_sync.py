from __future__ import annotations

import time

import pytest

from withings_sync import sync


def test_decode_measure_negative_exponent() -> None:
    assert sync.decode_measure({"value": 81271, "unit": -3}) == pytest.approx(81.271)


def test_decode_measure_zero_unit_passthrough() -> None:
    assert sync.decode_measure({"value": 72, "unit": 0}) == 72.0
    assert sync.decode_measure({"value": 72}) == 72.0


def test_decode_measure_positive_exponent() -> None:
    assert sync.decode_measure({"value": 12, "unit": 2}) == 1200.0


def test_fetch_measures_normalizes_and_sorts_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    now = int(time.time())
    payload = {
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
            ]
        },
    }
    calls = []

    def fake_post_form(path, data, token=None):
        calls.append((path, data, token))
        return payload

    monkeypatch.setattr(sync, "post_form", fake_post_form)

    result = sync.fetch_measures("access-token", 30)

    assert calls[0][0] == "/measure"
    assert calls[0][1]["action"] == "getmeas"
    assert calls[0][2] == "access-token"
    assert [e["grpid"] for e in result] == [2, 1]
    newest, oldest = result
    assert newest["weight_kg"] == pytest.approx(81.271)
    assert newest["fat_free_mass_kg"] == pytest.approx(60.5)
    assert newest["fat_ratio_pct"] == pytest.approx(25.3)
    assert newest["fat_mass_kg"] == pytest.approx(20.771)
    assert newest["muscle_mass_kg"] == pytest.approx(57.4)
    assert newest["visceral_fat_index"] == 7.0
    assert newest["timestamp"] == now - 60
    assert oldest["weight_kg"] == pytest.approx(80.0)
    assert "fat_ratio_pct" not in oldest


def test_fetch_activity_degrades_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: {"status": 123, "body": {}})
    assert sync.fetch_activity("access-token", 30) == [{"error": 123, "body": {}}]


def test_fetch_sleep_degrades_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: {"status": 123, "body": {}})
    assert sync.fetch_sleep("access-token", 30) == [{"error": 123, "body": {}}]


def test_summarize_picks_newest_entry_per_key_and_counts() -> None:
    measures = [
        {"date": "new", "weight_kg": 81.0, "visceral_fat_index": 7.0},
        {"date": "old", "weight_kg": 80.0, "fat_ratio_pct": 25.0},
    ]
    activity = [{"date": "2026-10-14", "steps": 100}, {"date": "2026-10-13", "steps": 50}]
    sleep = [{"date": "2026-10-13", "data": {"sleep_score": 85}}]

    result = sync.summarize(measures, activity, sleep)

    assert result["latest_measures"] == {
        "weight_kg": {"value": 81.0, "date": "new"},
        "fat_ratio_pct": {"value": 25.0, "date": "old"},
        "visceral_fat_index": {"value": 7.0, "date": "new"},
    }
    assert result["latest_activity"] == activity[0]
    assert result["latest_sleep"] == sleep[0]
    assert result["measure_count"] == 2
    assert result["activity_count"] == 2
    assert result["sleep_count"] == 1


def test_summarize_handles_empty_inputs() -> None:
    result = sync.summarize([], [], [])
    assert result["latest_measures"] == {}
    assert result["latest_activity"] is None
    assert result["latest_sleep"] is None
