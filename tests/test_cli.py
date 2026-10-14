from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from withings_agent_sync import __main__ as cli
from withings_agent_sync import storage, sync


def setup_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    token_file = tmp_path / "tokens.json"
    out_dir = tmp_path / "out"
    storage.save_json_secure(
        token_file,
        {"access_token": "access", "refresh_token": "refresh", "expires_at": int(time.time()) + 7200, "userid": 42},
    )
    monkeypatch.setenv("WITHINGS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("WITHINGS_OUT_DIR", str(out_dir))
    return token_file, out_dir


def stub_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "fetch_measures", lambda *a: [])
    monkeypatch.setattr(sync, "fetch_activity", lambda *a: [])
    monkeypatch.setattr(sync, "fetch_sleep", lambda *a: [])


def test_help_works_with_invalid_days_environment(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("WITHINGS_SYNC_DAYS", "invalid")
    with pytest.raises(SystemExit) as result:
        cli.main(["--help"])
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_invalid_days_and_port_are_usage_errors() -> None:
    for args in (["sync", "--days", "-1"], ["authorize", "--port", "0"], ["authorize", "--port", "65536"]):
        with pytest.raises(SystemExit) as result:
            cli.main(args)
        assert result.value.code == 2


def test_missing_credentials_is_config_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WITHINGS_CREDS_FILE", str(tmp_path / "missing-client.json"))
    assert cli.main(["authorize"]) == 2


def test_json_array_token_file_is_config_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "tokens.json"
    token_file.write_text("[]", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("WITHINGS_TOKEN_FILE", str(token_file))
    assert cli.main(["sync"]) == 2


def test_refresh_only_token_file_is_recoverable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "tokens.json"
    storage.save_json_secure(token_file, {"refresh_token": "refresh"})
    monkeypatch.setenv("WITHINGS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "client")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        sync,
        "refresh_if_needed",
        lambda *a, **k: {"access_token": "access", "refresh_token": "refresh", "userid": 42},
    )
    monkeypatch.setattr(sync, "fetch_measures", lambda *a: [])
    monkeypatch.setattr(sync, "fetch_activity", lambda *a: [])
    monkeypatch.setattr(sync, "fetch_sleep", lambda *a: [])
    assert cli.main(["sync", "--quiet"]) == 0


def test_health_files_are_private_and_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, out_dir = setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    assert cli.main(["sync", "--quiet"]) == 0
    for name in ("withings_latest.json", "withings_sync_history.jsonl"):
        path = out_dir / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads((out_dir / "withings_latest.json").read_text(encoding="utf-8"))["summary"]["measure_count"] == 0


def test_snapshot_and_history_collision_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, out_dir = setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    assert cli.main(["sync", "--quiet", "--history", str(out_dir / "withings_latest.json")]) == 2


def test_quiet_suppresses_only_success_output(monkeypatch, tmp_path, capsys):
    setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    assert cli.main(["sync", "--quiet"]) == 0
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_partial_sync_is_explicit_even_when_quiet(monkeypatch, tmp_path, capsys):
    _, out_dir = setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    monkeypatch.setattr(sync, "fetch_activity", lambda *a: [{"error": 123, "body": {}}])
    assert cli.main(["sync", "--quiet"]) == cli.EXIT_PARTIAL
    output = capsys.readouterr()
    assert output.out == ""
    assert "activity" in output.err and "123" in output.err
    payload = storage.load_json(out_dir / "withings_latest.json")
    assert payload["schema_version"] == 1
    assert payload["status"] == "partial"
    assert payload["summary"]["activity_count"] == 0
    assert payload["summary"]["latest_activity"] is None
    history = json.loads((out_dir / "withings_sync_history.jsonl").read_text())
    assert history["status"] == "partial"
    assert history["schema_version"] == 1


def test_history_stores_dates_not_full_activity_or_sleep_records(monkeypatch, tmp_path):
    _, out_dir = setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    monkeypatch.setattr(sync, "fetch_activity", lambda *a: [{"date": "2026-10-14", "steps": 4321}])
    monkeypatch.setattr(sync, "fetch_sleep", lambda *a: [{"date": "2026-10-13", "sleep_score": 81}])
    assert cli.main(["sync", "--quiet"]) == 0
    snapshot = storage.load_json(out_dir / "withings_latest.json")
    assert snapshot["summary"]["latest_sleep"] == {"date": "2026-10-13", "sleep_score": 81}
    history = json.loads((out_dir / "withings_sync_history.jsonl").read_text())
    assert history["summary"] == {
        "activity_count": 1,
        "latest_activity_date": "2026-10-14",
        "latest_measures": {},
        "latest_sleep_date": "2026-10-13",
        "measure_count": 0,
        "sleep_count": 1,
    }


def test_authentication_failure_warning_tells_user_to_reauthorize(monkeypatch, tmp_path, capsys):
    setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    monkeypatch.setattr(sync, "fetch_sleep", lambda *a: [{"error": 401, "body": {}}])
    assert cli.main(["sync", "--quiet"]) == cli.EXIT_PARTIAL
    assert "authorize again" in capsys.readouterr().err


@pytest.mark.parametrize("use_flag", [False, True])
def test_output_directory_cannot_contain_tokens(monkeypatch, tmp_path, capsys, use_flag):
    setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    args = ["sync", "--quiet"]
    if use_flag:
        args += ["--out", str(tmp_path)]
    else:
        monkeypatch.setenv("WITHINGS_OUT_DIR", str(tmp_path))
    assert cli.main(args) == 2
    assert "share with agents" in capsys.readouterr().err
    assert not (tmp_path / "withings_latest.json").exists()


def test_output_directory_cannot_contain_client_credentials(monkeypatch, tmp_path, capsys):
    _, out_dir = setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    monkeypatch.setenv("WITHINGS_CREDS_FILE", str(out_dir / "nested" / "client.json"))
    assert cli.main(["sync", "--quiet"]) == 2
    assert "credentials file" in capsys.readouterr().err


def test_default_data_directory_contains_no_secrets(tmp_path):
    script = (
        "from withings_agent_sync import sync; "
        "print(sync.DEFAULT_TOKEN_FILE.is_relative_to(sync.DEFAULT_OUT_DIR), "
        "sync.DEFAULT_CREDS_FILE.is_relative_to(sync.DEFAULT_OUT_DIR), sync.DEFAULT_OUT_DIR)"
    )
    environment = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == f"False False {tmp_path / '.local/share/withings-agent-sync'}"


@pytest.mark.parametrize("value", ["invalid", "0", "-1", "999999999999"])
def test_invalid_days_environment_is_a_config_error(monkeypatch, value):
    monkeypatch.setenv("WITHINGS_SYNC_DAYS", value)
    assert cli.main(["sync"]) == 2


def test_explicit_days_overrides_invalid_environment(monkeypatch, tmp_path):
    setup_token(monkeypatch, tmp_path)
    stub_fetches(monkeypatch)
    monkeypatch.setenv("WITHINGS_SYNC_DAYS", "invalid")
    assert cli.main(["sync", "--days", "5", "--quiet"]) == 0


def test_credentials_and_history_cannot_alias(monkeypatch, tmp_path):
    setup_token(monkeypatch, tmp_path)
    credentials = tmp_path / "client.json"
    storage.save_json_secure(credentials, {"client_id": "dummy", "client_secret": "dummy"})
    monkeypatch.setenv("WITHINGS_CREDS_FILE", str(credentials))
    assert cli.main(["sync", "--history", str(credentials)]) == 2
    assert storage.load_json(credentials) == {"client_id": "dummy", "client_secret": "dummy"}


def test_incomplete_history_stops_before_api_calls(monkeypatch, tmp_path):
    _, out_dir = setup_token(monkeypatch, tmp_path)
    out_dir.mkdir()
    history = out_dir / "withings_sync_history.jsonl"
    history.write_bytes(b'{"unfinished":')
    monkeypatch.setattr(sync, "fetch_measures", lambda *a: pytest.fail("must fail before fetching"))
    assert cli.main(["sync"]) == 2
    assert history.read_bytes() == b'{"unfinished":'


def test_runtime_failure_preserves_existing_snapshot(monkeypatch, tmp_path):
    _, out_dir = setup_token(monkeypatch, tmp_path)
    latest = out_dir / "withings_latest.json"
    storage.save_json_secure(latest, {"previous": True})

    def fail(*args):
        raise sync.APIError("simulated API failure")

    monkeypatch.setattr(sync, "fetch_measures", fail)
    assert cli.main(["sync"]) == 1
    assert storage.load_json(latest) == {"previous": True}


def test_busy_sync_fails_before_loading_or_refreshing_tokens(monkeypatch, tmp_path):
    token_file, _ = setup_token(monkeypatch, tmp_path)
    monkeypatch.setattr(sync, "load_json", lambda *a: pytest.fail("must lock before reading"))
    with storage.exclusive_locks([token_file]):
        assert cli.main(["sync"]) == 1


def test_no_browser_option_is_forwarded(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.auth, "run_authorize", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["authorize", "--no-browser", "--port", "8384"]) == 0
    assert calls == [{"port": 8384, "open_browser": False}]


def test_redirect_uri_option_is_forwarded(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.auth, "run_authorize", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["authorize", "--redirect-uri", "https://tower.example/callback", "--no-browser"]) == 0
    assert calls == [{"port": 8383, "open_browser": False, "redirect_uri": "https://tower.example/callback"}]
