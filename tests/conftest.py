from __future__ import annotations

import pytest

from withings_sync import sync

ENV_VARS = (
    "WITHINGS_TOKEN_FILE",
    "WITHINGS_CREDS_FILE",
    "WITHINGS_OUT_DIR",
    "WITHINGS_SYNC_DAYS",
    "WITHINGS_CLIENT_ID",
    "WITHINGS_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(sync, "urlopen", no_network)
