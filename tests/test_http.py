from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from withings_agent_sync import sync


def test_post_form_preserves_payload_host_and_bearer_header(monkeypatch):
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(b'{"status": 0, "body": {}}')

    monkeypatch.setattr(sync, "urlopen", open_request)
    assert sync.post_form("/measure", {"action": "getmeas", "offset": None}, "dummy-access") == {"status": 0, "body": {}}
    request, timeout = calls[0]
    assert request.full_url == "https://wbsapi.withings.net/measure"
    assert request.get_method() == "POST"
    assert parse_qs(request.data.decode()) == {"action": ["getmeas"]}
    assert request.get_header("Authorization") == "Bearer dummy-access"
    assert timeout == 30


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirects_are_rejected_without_forwarding_credentials(monkeypatch, code):
    handler = sync._NoRedirectHandler()
    body = io.BytesIO(b'never-forward-this-response')

    def redirect(request, timeout):
        getattr(handler, f"http_error_{code}")(
            request, body, code, "Found", {"Location": "http://other-host.invalid/target"}
        )

    monkeypatch.setattr(sync, "urlopen", redirect)
    with pytest.raises(sync.APIError, match="Refused HTTP redirect"):
        sync.post_form("/v2/oauth2", {"client_secret": "dummy-secret"}, "dummy-access")
    assert body.closed


def test_http_errors_do_not_echo_response_bodies_or_status_messages(monkeypatch):
    marker = "DO_NOT_LOG_DUMMY_CREDENTIAL"
    body = io.BytesIO(json.dumps({"access_token": marker}).encode())

    def fail(request, timeout):
        raise HTTPError(request.full_url, 400, marker, {}, body)

    monkeypatch.setattr(sync, "urlopen", fail)
    with pytest.raises(sync.APIError) as error:
        sync.post_form("/v2/oauth2", {})
    assert "HTTP 400" in str(error.value)
    assert marker not in str(error.value)
    assert body.closed


def test_network_errors_do_not_echo_untrusted_reason_text(monkeypatch):
    marker = "DO_NOT_LOG_DUMMY_CREDENTIAL"

    def fail(request, timeout):
        raise URLError(marker)

    monkeypatch.setattr(sync, "urlopen", fail)
    with pytest.raises(sync.APIError) as error:
        sync.post_form("/measure", {})
    assert marker not in str(error.value)


@pytest.mark.parametrize("data", [b"not-json", b"[]", b"null", b'{"value": NaN}', b"\xff"])
def test_malformed_responses_are_api_errors(monkeypatch, data):
    monkeypatch.setattr(sync, "urlopen", lambda *a, **k: io.BytesIO(data))
    with pytest.raises(sync.APIError):
        sync.post_form("/measure", {})


def test_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr(sync, "MAX_RESPONSE_BYTES", 10)
    monkeypatch.setattr(sync, "urlopen", lambda *a, **k: io.BytesIO(b"x" * 11))
    with pytest.raises(sync.APIError, match="size limit"):
        sync.post_form("/measure", {})
