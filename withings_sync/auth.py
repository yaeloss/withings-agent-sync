"""First-run OAuth2 authorization for Withings."""

from __future__ import annotations

import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from . import sync

AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
REDIRECT_HOST = "127.0.0.1"
DEFAULT_SCOPE = "user.info,user.metrics,user.activity,user.sleep"


class ConfigError(RuntimeError):
    """Raised when authorization configuration is missing or invalid."""


def post_form(path: str, data: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    """Keep the HTTP seam patchable at either the auth or sync module."""
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
    data = post_form(
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
    if data.get("status") != 0:
        raise RuntimeError(f"Authorization failed: status={data.get('status')} body={data.get('body')}")
    body = data["body"]
    now = int(time.time())
    return {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": now + int(body.get("expires_in", 10800)),
        "userid": body["userid"],
        "saved_at": now,
    }


def parse_callback(path: str, expected_state: str) -> str:
    query = parse_qs(urlsplit(path).query)
    state = query.get("state", [None])[0]
    if state != expected_state:
        raise RuntimeError("OAuth callback state did not match; authorization was rejected")
    error = query.get("error", [None])[0]
    if error:
        description = query.get("error_description", [error])[0]
        raise RuntimeError(f"Withings authorization failed: {description}")
    code = query.get("code", [None])[0]
    if not code:
        raise RuntimeError("OAuth callback did not contain an authorization code")
    return code


class CallbackServer:
    """Small localhost server that captures one OAuth callback."""

    def __init__(self, port: int, expected_state: str) -> None:
        result: dict[str, Any] = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    result["code"] = parse_callback(self.path, expected_state)
                    message = "Authorization complete — you may close this tab"
                    status = 200
                except RuntimeError as exc:
                    result["error"] = exc
                    message = str(exc)
                    status = 400
                body = message.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._result = result
        self._server = HTTPServer((REDIRECT_HOST, port), CallbackHandler)
        self._server.timeout = 300

    def __enter__(self) -> "CallbackServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.server_close()

    def wait_for_code(self, timeout: int = 300) -> str:
        self._server.timeout = timeout
        self._server.handle_request()
        if "error" in self._result:
            raise RuntimeError(str(self._result["error"]))
        if "code" not in self._result:
            raise RuntimeError("Timed out waiting for the Withings authorization callback")
        return self._result["code"]


def run_authorize(
    creds_file: Path | str | None = None,
    token_file: Path | str | None = None,
    port: int = 8383,
) -> None:
    creds_path = Path(creds_file).expanduser() if creds_file is not None else sync.default_creds_file()
    token_path = Path(token_file).expanduser() if token_file is not None else sync.default_token_file()
    client_id, client_secret = sync.load_client_credentials(creds_path)
    if not client_id or not client_secret:
        raise ConfigError(
            f"Missing Withings client credentials. Create {creds_path} with client_id/client_secret "
            "or set WITHINGS_CLIENT_ID and WITHINGS_CLIENT_SECRET."
        )

    redirect_uri = f"http://{REDIRECT_HOST}:{port}/callback"
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(client_id, redirect_uri, DEFAULT_SCOPE, state)
    try:
        server = CallbackServer(port, state)
    except OSError as exc:
        raise RuntimeError(f"Could not listen on port {port}; choose another port with --port") from exc

    with server:
        print(f"Open this URL to authorize Withings: {url}")
        print(f"Waiting for the callback at {redirect_uri} ...")
        webbrowser.open(url)
        code = server.wait_for_code(timeout=300)

    tokens = exchange_code(client_id, client_secret, code, redirect_uri)
    sync.save_json_secure(token_path, tokens)
    print(f"Saved Withings tokens to {token_path}")
