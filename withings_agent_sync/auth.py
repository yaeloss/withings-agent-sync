"""First-run OAuth2 authorization using a bounded loopback callback server."""

from __future__ import annotations

import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from . import sync
from .errors import APIError, ConfigError
from .storage import exclusive_locks, validate_paths

AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
REDIRECT_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
# user.activity covers sleep summaries; user.sleep is not a Public API scope.
DEFAULT_SCOPE = "user.info,user.metrics,user.activity"
CALLBACK_TIMEOUT = 300


class CallbackError(APIError):
    """A valid callback reported denial, or the callback wait failed."""


class CallbackRejected(CallbackError):
    """Ignore malformed or unrelated callbacks while waiting for the real one."""


class CallbackStateError(CallbackRejected):
    """A callback did not have the expected CSRF state."""


def post_form(path: str, data: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    return sync.post_form(path, data, token)


def build_authorize_url(client_id: str, redirect_uri: str, scope: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict[str, Any]:
    response = post_form(
        "/v2/oauth2",
        {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    body = sync._response_body(response, "Authorization")
    return sync._make_token_payload(body, now=int(time.time()))


def parse_callback(path: str, expected_state: str) -> str:
    try:
        parsed = urlsplit(path)
        if parsed.path != CALLBACK_PATH or parsed.scheme or parsed.netloc or parsed.fragment:
            raise CallbackRejected("OAuth callback used an unexpected path")
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
    except ValueError as exc:
        raise CallbackRejected("Malformed OAuth callback") from exc
    states = query.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0].encode(), expected_state.encode()):
        raise CallbackStateError("OAuth callback state did not match; callback rejected")
    errors = query.get("error", [])
    codes = query.get("code", [])
    if errors:
        if len(errors) != 1 or not errors[0] or codes:
            raise CallbackRejected("Ambiguous OAuth callback")
        known_errors = {"access_denied", "invalid_scope", "server_error", "temporarily_unavailable"}
        label = errors[0] if errors[0] in known_errors else "provider_error"
        # Never echo error_description: the provider or a local requester can
        # put credentials, personal information, or terminal controls in it.
        raise CallbackError(f"Withings authorization failed ({label}); run authorize again.")
    if len(codes) != 1 or not codes[0] or len(codes[0]) > 2048:
        raise CallbackRejected("OAuth callback did not contain one authorization code")
    if any(ord(char) < 32 for char in codes[0]):
        raise CallbackRejected("Invalid authorization code")
    return codes[0]


class _CallbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        self.deadline: float | None = None
        self._connections: set[socket.socket] = set()
        self._connection_lock = threading.Lock()
        super().__init__(address, handler)

    def get_request(self):
        connection, address = super().get_request()
        remaining = 5.0 if self.deadline is None else self.deadline - time.monotonic()
        with self._connection_lock:
            if remaining <= 0 or len(self._connections) >= 16:
                connection.close()
                raise OSError("Callback connection rejected")
            connection.settimeout(min(5.0, remaining))
            self._connections.add(connection)
        return connection, address

    def shutdown_request(self, connection: socket.socket) -> None:
        try:
            super().shutdown_request(connection)
        finally:
            with self._connection_lock:
                self._connections.discard(connection)

    def server_close(self) -> None:
        with self._connection_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Malformed local HTTP requests must not print OAuth query strings.
        return


class CallbackServer:
    """Ignore stray requests; accept one validated result before an absolute deadline."""

    def __init__(self, port: int, expected_state: str) -> None:
        self._event = threading.Event()
        self._result: dict[str, Any] = {}
        self._result_lock = threading.Lock()
        owner = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    parsed = urlsplit(self.path)
                except ValueError:
                    owner._respond(self, 400, "Invalid request")
                    return
                if parsed.path != CALLBACK_PATH:
                    owner._respond(self, 404, "Not found")
                    return
                try:
                    code = parse_callback(self.path, expected_state)
                except CallbackRejected:
                    owner._respond(self, 400, "Authorization callback rejected")
                    return
                except CallbackError as exc:
                    accepted = owner._store_result("error", exc)
                    status, message = 400, "Authorization denied. Check the terminal."
                else:
                    accepted = owner._store_result("code", code)
                    status = 200
                    message = "Authorization response received. Check the terminal to confirm tokens were saved."
                if not accepted:
                    owner._respond(self, 409, "An authorization response was already received")
                    return
                try:
                    owner._respond(self, status, message)
                finally:
                    owner._event.set()

            def do_POST(self) -> None:
                owner._respond(self, 405, "Method not allowed")

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = _CallbackHTTPServer((REDIRECT_HOST, port), CallbackHandler)

    def _store_result(self, key: str, value: Any) -> bool:
        with self._result_lock:
            if self._result:
                return False
            self._result[key] = value
            return True

    @staticmethod
    def _respond(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
        body = message.encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address

    def __enter__(self) -> "CallbackServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.server_close()

    def wait_for_code(self, timeout: float = CALLBACK_TIMEOUT) -> str:
        if timeout <= 0:
            raise ConfigError("Callback timeout must be positive")
        deadline = time.monotonic() + timeout
        self._server.deadline = deadline
        while not self._event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CallbackError("Timed out waiting for Withings. Run authorize again and check the callback URL.")
            self._server.timeout = min(0.1, remaining)
            self._server.handle_request()
        if "error" in self._result:
            raise self._result["error"]
        return self._result["code"]


def _open_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url)
    except (webbrowser.Error, OSError):
        opened = False
    if not opened:
        print("Could not open a browser; open the printed URL manually.", file=sys.stderr)


def _launch_browser(url: str) -> None:
    # Some text browsers block webbrowser.open(). Do not let that prevent code
    # capture and exchange while the Withings authorization code is still valid.
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()


def run_authorize(
    creds_file: Path | str | None = None,
    token_file: Path | str | None = None,
    port: int = 8383,
    open_browser: bool = True,
) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("Callback port must be between 1 and 65535")
    creds_path = Path(creds_file).expanduser() if creds_file is not None else sync.default_creds_file()
    token_path = Path(token_file).expanduser() if token_file is not None else sync.default_token_file()
    validate_paths({"credentials": creds_path, "token": token_path}, locked=("token",))
    with exclusive_locks([token_path]):
        client_id, client_secret = sync.load_client_credentials(creds_path)
        if not client_id or not client_secret:
            raise ConfigError(
                f"Missing Withings client credentials. Create {creds_path} with client_id/client_secret "
                "or set WITHINGS_CLIENT_ID and WITHINGS_CLIENT_SECRET."
            )
        redirect_uri = f"http://{REDIRECT_HOST}:{port}{CALLBACK_PATH}"
        state = secrets.token_urlsafe(16)
        url = build_authorize_url(client_id, redirect_uri, DEFAULT_SCOPE, state)
        try:
            server = CallbackServer(port, state)
        except OSError as exc:
            raise ConfigError(
                f"Could not listen on port {port}. Choose another with --port and register its callback URL."
            ) from exc
        with server:
            print(f"Open this URL to authorize Withings: {url}", flush=True)
            print(f"Waiting up to five minutes at {redirect_uri} ...", flush=True)
            if open_browser:
                _launch_browser(url)
            code = server.wait_for_code()
        tokens = exchange_code(client_id, client_secret, code, redirect_uri)
        sync.save_json_secure(token_path, tokens)
        print(f"Saved Withings tokens to {token_path}")
