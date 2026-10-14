from __future__ import annotations

import socket

import pytest

from withings_agent_sync import auth, sync


ENV_VARS = (
    "WITHINGS_TOKEN_FILE",
    "WITHINGS_CREDS_FILE",
    "WITHINGS_OUT_DIR",
    "WITHINGS_SYNC_DAYS",
    "WITHINGS_CLIENT_ID",
    "WITHINGS_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sync, "DEFAULT_TOKEN_FILE", tmp_path / "state" / "tokens.json")
    monkeypatch.setattr(sync, "DEFAULT_CREDS_FILE", tmp_path / "config" / "client.json")
    monkeypatch.setattr(sync, "DEFAULT_OUT_DIR", tmp_path / "data")

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(sync, "urlopen", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(auth.webbrowser, "open", no_network)
