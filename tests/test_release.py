from __future__ import annotations

import io
import json
import socket
import threading
import time
from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener

import pytest

from withings_agent_sync import __main__ as cli
from withings_agent_sync import storage, sync

CREATE_CONNECTION = socket.create_connection


@pytest.mark.parametrize(
    "error",
    [BadStatusLine("DUMMY_PRIVATE_STATUS_LINE"), IncompleteRead(b"dummy-partial-body"), RemoteDisconnected()],
)
def test_http_protocol_failures_are_safe_api_errors(monkeypatch, error) -> None:
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(sync, "urlopen", fail)
    with pytest.raises(sync.APIError, match="failed or timed out") as result:
        sync.post_form("/measure", {})
    assert "DUMMY_PRIVATE_STATUS_LINE" not in str(result.value)
    assert "dummy-partial-body" not in str(result.value)


@pytest.mark.parametrize("path", ["measure", "//other-host.invalid/measure", "/measure?query=private", "/measure#fragment"])
def test_endpoint_paths_cannot_override_the_api_host(monkeypatch, path: str) -> None:
    monkeypatch.setattr(sync, "urlopen", lambda *a, **k: pytest.fail("invalid endpoint must not make a request"))
    with pytest.raises(sync.APIError, match="endpoint path"):
        sync.post_form(path, {})


@pytest.mark.parametrize("response", [[], {}, {"status": True}, {"status": "0"}, {"status": 0, "body": []}])
def test_invalid_api_response_shapes_are_rejected(monkeypatch, response) -> None:
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: response)
    with pytest.raises(sync.APIError):
        sync.fetch_measures("dummy-access", 30)


@pytest.mark.parametrize("days", [0, -1, True, 1.5, "30", 10**20])
@pytest.mark.parametrize("fetcher", [sync.fetch_measures, sync.fetch_activity, sync.fetch_sleep])
def test_library_fetchers_reject_invalid_date_ranges(fetcher, days) -> None:
    with pytest.raises(sync.ConfigError):
        fetcher("dummy-access", days)


@pytest.mark.parametrize("value", [None, True, 0, -1, "0", "not-numeric", "42\n", "４２"])
def test_invalid_user_ids_cannot_be_saved_from_a_token_response(value) -> None:
    with pytest.raises(sync.APIError, match="userid"):
        sync._make_token_payload(
            {"access_token": "dummy-access", "refresh_token": "dummy-refresh", "expires_in": 10800, "userid": value}
        )


@pytest.mark.parametrize("field", ["access_token", "refresh_token"])
@pytest.mark.parametrize("value", [False, [], "", "dummy\nprivate"])
def test_corrupted_local_token_fields_are_configuration_errors(field: str, value) -> None:
    tokens = {"access_token": "dummy-access", "refresh_token": "dummy-refresh", "expires_at": int(time.time()) + 7200}
    tokens[field] = value
    with pytest.raises(sync.ConfigError, match=field):
        sync.refresh_if_needed(tokens)


def test_partial_sync_is_explicit_without_printing_provider_details(monkeypatch, tmp_path, capsys) -> None:
    token_file = tmp_path / "tokens.json"
    out_dir = tmp_path / "out"
    storage.save_json_secure(
        token_file,
        {"access_token": "dummy-access", "refresh_token": "dummy-refresh", "expires_at": int(time.time()) + 7200, "userid": 42},
    )
    monkeypatch.setenv("WITHINGS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("WITHINGS_OUT_DIR", str(out_dir))
    monkeypatch.setattr(sync, "fetch_measures", lambda *a: [])
    monkeypatch.setattr(sync, "fetch_activity", lambda *a: [{"date": "2026-10-14", "steps": 123}])
    monkeypatch.setattr(sync, "fetch_sleep", lambda *a: [{"error": 123, "body": {"details": "DUMMY_PRIVATE_DETAILS"}}])

    assert cli.main(["sync", "--quiet"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Warning: sleep is incomplete (Withings status 123).\n"
    snapshot = storage.load_json(out_dir / cli.SNAPSHOT_NAME)
    history = json.loads((out_dir / cli.HISTORY_NAME).read_text())
    assert snapshot["schema_version"] == history["schema_version"] == 1
    assert snapshot["status"] == history["status"] == "partial"
    assert snapshot["summary"]["activity_count"] == 1
    assert snapshot["summary"]["sleep_count"] == 0


def test_incomplete_history_is_rejected_before_refresh_or_fetch(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "tokens.json"
    out_dir = tmp_path / "out"
    storage.save_json_secure(token_file, {"refresh_token": "dummy-refresh"})
    out_dir.mkdir()
    history = out_dir / cli.HISTORY_NAME
    history.write_bytes(b'{"unfinished":')
    monkeypatch.setenv("WITHINGS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("WITHINGS_OUT_DIR", str(out_dir))
    monkeypatch.setattr(sync, "refresh_if_needed", lambda *a, **k: pytest.fail("history must be checked first"))
    assert cli.main(["sync", "--quiet"]) == 2
    assert history.read_bytes() == b'{"unfinished":'
    assert not (out_dir / cli.SNAPSHOT_NAME).exists()


def test_network_failure_does_not_replace_existing_snapshot_or_history(monkeypatch, tmp_path, capsys) -> None:
    token_file = tmp_path / "tokens.json"
    out_dir = tmp_path / "out"
    storage.save_json_secure(
        token_file,
        {"access_token": "dummy-access", "refresh_token": "dummy-refresh", "expires_at": int(time.time()) + 7200},
    )
    storage.save_json_secure(out_dir / cli.SNAPSHOT_NAME, {"previous": True})
    storage.append_jsonl_secure(out_dir / cli.HISTORY_NAME, {"previous": True})
    before = [(out_dir / name).read_bytes() for name in (cli.SNAPSHOT_NAME, cli.HISTORY_NAME)]
    monkeypatch.setenv("WITHINGS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("WITHINGS_OUT_DIR", str(out_dir))

    def failing_fetch(*args):
        raise sync.APIError("Simulated API failure")

    monkeypatch.setattr(sync, "fetch_measures", failing_fetch)
    assert cli.main(["sync", "--quiet"]) == 1
    assert capsys.readouterr().err == "Error: Simulated API failure\n"
    assert [(out_dir / name).read_bytes() for name in (cli.SNAPSHOT_NAME, cli.HISTORY_NAME)] == before
    with storage.exclusive_locks([token_file, out_dir / cli.SNAPSHOT_NAME, out_dir / cli.HISTORY_NAME]):
        pass


def test_post_form_decodes_utf8_without_leaking_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(sync, "urlopen", lambda *a, **k: io.BytesIO(b'{"private": "DUMMY_PRIVATE_JSON"'))
    with pytest.raises(sync.APIError, match="Invalid JSON response") as result:
        sync.post_form("/measure", {})
    assert "DUMMY_PRIVATE_JSON" not in str(result.value)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_real_http_opener_does_not_follow_redirects(monkeypatch, code: int) -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            requests.append((self.path, self.headers.get("Authorization"), self.rfile.read(length)))
            self.send_response(code)
            self.send_header("Location", "/forwarded")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"), b""))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    def loopback_connection(address, *args, **kwargs):
        assert address[0] == "127.0.0.1", "outbound network connections are forbidden"
        return CREATE_CONNECTION(address, *args, **kwargs)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        monkeypatch.setattr(socket, "create_connection", loopback_connection)
        monkeypatch.setattr(sync, "API_BASE", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(sync, "_HTTP_OPENER", build_opener(ProxyHandler({}), sync._NoRedirectHandler()))
        monkeypatch.setattr(sync, "urlopen", sync._safe_urlopen)
        try:
            with pytest.raises(sync.APIError, match="Refused HTTP redirect"):
                sync.post_form("/v2/oauth2", {"client_secret": "dummy-secret"}, "dummy-access")
        finally:
            server.shutdown()
            worker.join(timeout=3)
        assert not worker.is_alive()
    assert requests == [("/v2/oauth2", "Bearer dummy-access", b"client_secret=dummy-secret")]


@pytest.mark.parametrize("more", [None, "1", 2, []])
def test_invalid_pagination_flags_are_rejected(monkeypatch, more) -> None:
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: {"status": 0, "body": {"activities": [], "more": more}})
    with pytest.raises(sync.APIError, match="pagination flag"):
        sync.fetch_activity("dummy-access", 30)


@pytest.mark.parametrize("offset", [None, "1", True, 0, -1])
def test_invalid_pagination_offsets_are_rejected(monkeypatch, offset) -> None:
    monkeypatch.setattr(
        sync,
        "post_form",
        lambda *a, **k: {"status": 0, "body": {"series": [], "more": True, "offset": offset}},
    )
    with pytest.raises(sync.APIError, match="non-progressing"):
        sync.fetch_sleep("dummy-access", 30)


def test_endless_pagination_has_a_fixed_request_limit(monkeypatch) -> None:
    calls = []

    def more_pages(path, params, token):
        calls.append(params)
        return {"status": 0, "body": {"measuregrps": [], "more": 1, "offset": params.get("offset", 0) + 1}}

    monkeypatch.setattr(sync, "MAX_PAGES", 2)
    monkeypatch.setattr(sync, "post_form", more_pages)
    with pytest.raises(sync.APIError, match="pagination limit"):
        sync.fetch_measures("dummy-access", 30)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "measure",
    [
        {},
        {"value": None},
        {"value": True},
        {"value": "invalid"},
        {"value": "nan"},
        {"value": 1, "unit": False},
        {"value": 1, "unit": 1.5},
        {"value": 1, "unit": 309},
        {"value": 1, "unit": -999},
    ],
)
def test_malformed_measurements_cannot_silently_become_health_values(measure) -> None:
    with pytest.raises(sync.APIError):
        sync.decode_measure(measure)


@pytest.mark.parametrize(
    "changes",
    [
        {"date": None},
        {"date": True},
        {"date": 1.5},
        {"date": 10**20},
        {"grpid": True},
        {"measures": {}},
        {"measures": [None]},
        {"measures": [{"type": True, "value": 1}]},
        {"measures": [{"type": 1.5, "value": 1}]},
        {"measures": [{"type": None, "value": 1}]},
    ],
)
def test_invalid_measure_groups_are_api_errors(monkeypatch, changes) -> None:
    group = {"date": 1700000000, "grpid": 1, "category": 1, "measures": []}
    group.update(changes)
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: {"status": 0, "body": {"measuregrps": [group]}})
    with pytest.raises(sync.APIError):
        sync.fetch_measures("dummy-access", 30)


def test_duplicate_groups_and_user_objectives_do_not_pollute_measure_summary(monkeypatch) -> None:
    group = {"date": 1700000000, "grpid": 1, "category": 1, "measures": [{"type": 1, "value": 80000, "unit": -3}]}
    objective = {**group, "grpid": 2, "category": 2, "date": 1700000001}
    monkeypatch.setattr(
        sync, "post_form", lambda *a, **k: {"status": 0, "body": {"measuregrps": [group, group, objective]}}
    )
    measures = sync.fetch_measures("dummy-access", 30)
    summary = sync.summarize(measures + [objective], [], [])
    assert len(measures) == summary["measure_count"] == 1
    assert summary["latest_measures"]["weight_kg"]["value"] == 80.0


@pytest.mark.parametrize("value", [None, "2026-02-30", "20261014", True])
@pytest.mark.parametrize("fetcher,field", [(sync.fetch_activity, "activities"), (sync.fetch_sleep, "series")])
def test_invalid_daily_record_dates_are_api_errors(monkeypatch, value, fetcher, field) -> None:
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: {"status": 0, "body": {field: [{"date": value}]}})
    with pytest.raises(sync.APIError, match="valid date"):
        fetcher("dummy-access", 30)


@pytest.mark.parametrize("fetcher,field", [(sync.fetch_activity, "activities"), (sync.fetch_sleep, "series")])
def test_provider_records_cannot_inject_private_text_into_error_warnings(monkeypatch, fetcher, field) -> None:
    marker = "DUMMY_PRIVATE_PROVIDER_TEXT"
    record = {"date": "2026-10-14", "error": marker}
    monkeypatch.setattr(sync, "post_form", lambda *a, **k: {"status": 0, "body": {field: [record]}})
    with pytest.raises(sync.APIError, match="reserved error field") as result:
        fetcher("dummy-access", 30)
    assert marker not in str(result.value)


@pytest.mark.parametrize(
    "environment",
    [
        {"WITHINGS_CLIENT_ID": "dummy-client"},
        {"WITHINGS_CLIENT_SECRET": "dummy-secret"},
        {"WITHINGS_CLIENT_ID": "", "WITHINGS_CLIENT_SECRET": "dummy-secret"},
        {"WITHINGS_CLIENT_ID": "dummy-client", "WITHINGS_CLIENT_SECRET": " "},
        {"WITHINGS_CLIENT_ID": "dummy-client", "WITHINGS_CLIENT_SECRET": "dummy\nsecret"},
    ],
)
def test_incomplete_or_invalid_environment_credentials_are_rejected(monkeypatch, environment) -> None:
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(sync.ConfigError):
        sync.load_client_credentials()


def test_environment_credentials_take_precedence_over_the_credentials_file(monkeypatch, tmp_path) -> None:
    credentials = tmp_path / "client.json"
    credentials.write_bytes(b"invalid-private-file")
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "dummy-client")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "dummy-secret")
    assert sync.load_client_credentials(credentials) == ("dummy-client", "dummy-secret")


@pytest.mark.parametrize("expiry", [None, True, 1.5, "invalid", 0, -1])
def test_invalid_token_expiry_responses_are_rejected(expiry) -> None:
    with pytest.raises(sync.APIError, match="expires_in"):
        sync._make_token_payload(
            {"access_token": "dummy-access", "refresh_token": "dummy-refresh", "expires_in": expiry, "userid": 42}
        )


@pytest.mark.parametrize("expiry", [True, 1.5, "invalid", {}])
def test_invalid_saved_expiry_values_are_configuration_errors(expiry) -> None:
    with pytest.raises(sync.ConfigError, match="expires_at"):
        sync.refresh_if_needed({"access_token": "dummy-access", "refresh_token": "dummy-refresh", "expires_at": expiry})


def test_missing_token_file_is_a_helpful_configuration_error(capsys) -> None:
    assert cli.main(["sync", "--quiet"]) == 2
    assert "Run withings-agent-sync authorize first" in capsys.readouterr().err


def test_invalid_days_environment_is_a_configuration_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("WITHINGS_SYNC_DAYS", "invalid")
    assert cli.main(["sync", "--quiet"]) == 2
    assert "WITHINGS_SYNC_DAYS" in capsys.readouterr().err


def test_successful_sync_prints_summary_without_printing_tokens(monkeypatch, tmp_path, capsys) -> None:
    token_file = tmp_path / "tokens.json"
    out_dir = tmp_path / "out"
    storage.save_json_secure(
        token_file,
        {"access_token": "DUMMY_PRIVATE_TOKEN", "refresh_token": "dummy-refresh", "expires_at": int(time.time()) + 7200},
    )
    monkeypatch.setenv("WITHINGS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("WITHINGS_OUT_DIR", str(out_dir))
    monkeypatch.setenv("WITHINGS_SYNC_DAYS", "invalid")
    monkeypatch.setattr(sync, "fetch_measures", lambda *a: [{"date": "2026-10-14", "weight_kg": 80.0}])
    monkeypatch.setattr(sync, "fetch_activity", lambda *a: [])
    monkeypatch.setattr(sync, "fetch_sleep", lambda *a: [])
    assert cli.main(["sync", "--days", "7"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "measures=1 activity=0 sleep=0" in captured.out
    assert "weight_kg: 80.0 at 2026-10-14" in captured.out
    assert str(out_dir / cli.SNAPSHOT_NAME) in captured.out
    assert "DUMMY_PRIVATE_TOKEN" not in captured.out
    assert storage.load_json(out_dir / cli.SNAPSHOT_NAME)["days"] == 7
