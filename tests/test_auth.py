from __future__ import annotations

import stat
from urllib.parse import parse_qs, urlsplit

import pytest

from withings_agent_sync import auth, storage, sync

REDIRECT_URI = "http://127.0.0.1:8383/callback"


def test_default_scope_uses_current_documented_withings_scopes() -> None:
    assert auth.DEFAULT_SCOPE == "user.info,user.metrics,user.activity"


def test_build_authorize_url() -> None:
    url = auth.build_authorize_url("client id", REDIRECT_URI, auth.DEFAULT_SCOPE, "state-value")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://account.withings.com/oauth2_user/authorize2"
    assert parse_qs(parts.query) == {
        "response_type": ["code"],
        "client_id": ["client id"],
        "redirect_uri": [REDIRECT_URI],
        "scope": [auth.DEFAULT_SCOPE],
        "state": ["state-value"],
    }


def test_exchange_code_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_post_form(path, data, token=None):
        calls.append((path, data, token))
        return {
            "status": 0,
            "body": {"access_token": "access", "refresh_token": "refresh", "expires_in": 10800, "userid": 42},
        }

    monkeypatch.setattr(auth, "post_form", fake_post_form)
    monkeypatch.setattr(auth.time, "time", lambda: 1_000_000)
    result = auth.exchange_code("client", "secret", "code", REDIRECT_URI)
    assert calls[0] == (
        "/v2/oauth2",
        {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "code": "code",
            "client_id": "client",
            "client_secret": "secret",
            "redirect_uri": REDIRECT_URI,
        },
        None,
    )
    assert result == {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": 1_010_800,
        "userid": 42,
        "saved_at": 1_000_000,
    }


def test_exchange_code_raises_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "post_form", lambda *a, **k: {"status": 503, "body": {}})
    with pytest.raises(sync.APIError, match="status 503"):
        auth.exchange_code("client", "secret", "code", REDIRECT_URI)


def test_parse_callback_requires_callback_path_and_matching_state() -> None:
    assert auth.parse_callback("/callback?code=abc&state=expected", "expected") == "abc"
    with pytest.raises(auth.CallbackError, match="unexpected path"):
        auth.parse_callback("/not-callback?code=abc&state=expected", "expected")
    with pytest.raises(auth.CallbackStateError, match="state"):
        auth.parse_callback("/callback?code=abc&state=wrong", "expected")


def test_parse_callback_reports_provider_error_and_missing_code() -> None:
    with pytest.raises(auth.CallbackError, match="access_denied"):
        auth.parse_callback("/callback?error=access_denied&state=expected", "expected")
    with pytest.raises(auth.CallbackError, match="code"):
        auth.parse_callback("/callback?state=expected", "expected")


class FakeServer:
    instances: list["FakeServer"] = []

    def __init__(self, port, expected_state):
        self.port = port
        self.expected_state = expected_state
        self.closed = False
        FakeServer.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def wait_for_code(self):
        return "auth-code"


def test_run_authorize_writes_only_expected_private_fields(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    creds_file = tmp_path / "client.json"
    storage.save_json_secure(creds_file, {"client_id": "client", "client_secret": "secret"})
    token_file = tmp_path / "state" / "tokens.json"
    calls = []

    def fake_post_form(path, data, token=None):
        calls.append((path, data))
        return {
            "status": 0,
            "body": {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 10800,
                "userid": 42,
                "scope": "user.info,user.metrics,user.activity",
                "token_type": "Bearer",
            },
        }

    FakeServer.instances.clear()
    monkeypatch.setattr(auth, "CallbackServer", FakeServer)
    monkeypatch.setattr(auth, "post_form", fake_post_form)
    auth.run_authorize(creds_file=creds_file, token_file=token_file, port=8383, open_browser=False)

    server = FakeServer.instances[0]
    assert server.port == 8383
    assert server.closed
    out = capsys.readouterr().out
    url = out.splitlines()[0].split(": ", 1)[1]
    assert parse_qs(urlsplit(url).query)["state"] == [server.expected_state]
    assert calls[0][1]["code"] == "auth-code"
    assert calls[0][1]["redirect_uri"] == REDIRECT_URI
    saved = sync.load_json(token_file)
    assert set(saved) == {"access_token", "refresh_token", "expires_at", "userid", "saved_at"}
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert REDIRECT_URI in out


def test_run_authorize_without_credentials_is_config_error(tmp_path) -> None:
    with pytest.raises(auth.ConfigError, match="client.json"):
        auth.run_authorize(creds_file=tmp_path / "client.json", token_file=tmp_path / "tokens.json")
