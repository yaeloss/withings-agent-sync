from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest

from withings_agent_sync import auth
from withings_agent_sync.errors import ConfigError


def request(address: tuple[str, int], path: str, method: str = "GET") -> tuple[int, bytes]:
    """Use a raw loopback socket; the suite still blocks outbound HTTP calls."""
    assert address[0] == "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(address)
        connection.sendall(
            f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode("ascii")
        )
        chunks = []
        while chunk := connection.recv(4096):
            chunks.append(chunk)
    response = b"".join(chunks)
    return int(response.split(b" ", 2)[1]), response


@pytest.mark.parametrize(
    "path",
    [
        "/callback?code=first&code=second&state=expected",
        "/callback?code=code&state=expected&state=expected",
        "/callback?code=code&state=",
        "/callback?code=code",
        "/callback?code=&state=expected",
        "/callback?code=code%0Aprivate&state=expected",
        "/callback?code=code&state=expected&error=access_denied",
        "/callback?error=&state=expected",
        "/callback?error=access_denied&error=server_error&state=expected",
        "/callback?code=code&state=expected#fragment",
        "http://127.0.0.1/callback?code=code&state=expected",
        "//other-host.invalid/callback?code=code&state=expected",
        "http://[invalid/callback?code=code&state=expected",
        "/callback?code=" + "x" * 2049 + "&state=expected",
        "/callback?code=code&state=expected&" + "&".join(f"extra{i}=value" for i in range(9)),
    ],
)
def test_ambiguous_or_malformed_callbacks_are_rejected(path: str) -> None:
    with pytest.raises(auth.CallbackRejected):
        auth.parse_callback(path, "expected")


def test_provider_error_text_is_not_echoed() -> None:
    marker = "DUMMY_PRIVATE_ERROR_DESCRIPTION"
    path = "/callback?" + urlencode({"state": "expected", "error": marker, "error_description": marker})
    with pytest.raises(auth.CallbackError, match="provider_error") as result:
        auth.parse_callback(path, "expected")
    assert marker not in str(result.value)


def test_callback_server_ignores_stray_requests_and_accepts_valid_code(capsys) -> None:
    with auth.CallbackServer(0, "expected") as server, ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(server.wait_for_code, 3)
        assert request(server.address, "/favicon.ico")[0] == 404
        assert request(server.address, "/callback?code=stray&state=wrong")[0] == 400
        assert request(server.address, "/callback", method="POST")[0] == 405
        status, response = request(server.address, "/callback?code=dummy-code&state=expected")
        assert status == 200
        assert b"Cache-Control: no-store" in response
        assert b"Referrer-Policy: no-referrer" in response
        assert b"dummy-code" not in response
        assert result.result(timeout=3) == "dummy-code"
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_valid_denial_is_reported_and_does_not_log_private_description(capsys) -> None:
    with auth.CallbackServer(0, "expected") as server, ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(server.wait_for_code, 3)
        status, response = request(
            server.address,
            "/callback?error=access_denied&state=expected&error_description=DUMMY_PRIVATE_DESCRIPTION",
        )
        assert status == 400
        assert b"DUMMY_PRIVATE_DESCRIPTION" not in response
        with pytest.raises(auth.CallbackError, match="access_denied"):
            result.result(timeout=3)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_first_valid_callback_cannot_be_overwritten() -> None:
    with auth.CallbackServer(0, "expected") as server:
        assert server._store_result("code", "first")
        assert not server._store_result("code", "second")
        assert not server._store_result("error", auth.CallbackError("denied"))
        assert server._result == {"code": "first"}


def test_timeout_is_bounded_even_when_a_client_sends_no_http_request(monkeypatch) -> None:
    with auth.CallbackServer(0, "expected") as server, socket.socket() as idle:
        idle.settimeout(3)
        idle.connect(server.address)
        accepted = threading.Event()
        original_get_request = server._server.get_request

        def get_request():
            connection = original_get_request()
            accepted.set()
            return connection

        monkeypatch.setattr(server._server, "get_request", get_request)
        started = time.monotonic()
        with pytest.raises(auth.CallbackError, match="Timed out"):
            server.wait_for_code(timeout=0.1)
        assert accepted.is_set()
        assert time.monotonic() - started < 2
    assert not server._server._connections


@pytest.mark.parametrize("timeout", [0, -1])
def test_callback_wait_rejects_nonpositive_timeout(timeout: float) -> None:
    with auth.CallbackServer(0, "expected") as server:
        with pytest.raises(ConfigError, match="positive"):
            server.wait_for_code(timeout=timeout)


@pytest.mark.parametrize("port", [0, -1, 65536, True, "8383"])
def test_authorization_rejects_invalid_ports_before_loading_credentials(port) -> None:
    with pytest.raises(ConfigError, match="between 1 and 65535"):
        auth.run_authorize(port=port)


def test_browser_launch_returns_without_waiting_for_a_blocking_browser(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_browser(url):
        started.set()
        release.wait(timeout=3)
        finished.set()
        return True

    monkeypatch.setattr(auth.webbrowser, "open", blocking_browser)
    try:
        auth._launch_browser("https://account.withings.com/dummy")
        assert started.wait(timeout=2)
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(timeout=2)


@pytest.mark.parametrize("raises", [False, True])
def test_browser_failure_prints_a_safe_manual_fallback(monkeypatch, capsys, raises: bool) -> None:
    def failed_browser(url):
        if raises:
            raise OSError("DUMMY_PRIVATE_BROWSER_ERROR")
        return False

    monkeypatch.setattr(auth.webbrowser, "open", failed_browser)
    auth._open_browser("https://account.withings.com/dummy")
    assert capsys.readouterr().err == "Could not open a browser; open the printed URL manually.\n"
