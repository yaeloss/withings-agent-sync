from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from withings_agent_sync import storage
from withings_agent_sync.errors import BusyError, ConfigError


def test_atomic_file_is_private_before_any_content_is_written(monkeypatch, tmp_path):
    path = tmp_path / "state" / "tokens.json"
    fdopen = os.fdopen
    observed = []

    def observe(fd, *args, **kwargs):
        observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
        assert stat.S_IMODE(next(path.parent.glob(".tokens.json.*.tmp")).stat().st_mode) == 0o600
        return fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(storage.os, "fdopen", observe)
    storage.save_json_secure(path, {"access_token": "dummy"})
    assert observed == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("failure", ["fchmod", "fsync", "replace"])
def test_failed_save_preserves_previous_tokens(monkeypatch, tmp_path, failure):
    path = tmp_path / "tokens.json"
    previous = {"access_token": "old", "refresh_token": "old-refresh"}
    storage.save_json_secure(path, previous)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(storage.os, failure, fail)
    with pytest.raises(OSError):
        storage.save_json_secure(path, {"access_token": "new", "refresh_token": "new-refresh"})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".tokens.json.*.tmp"))


def test_invalid_json_payload_preserves_previous_snapshot(tmp_path):
    path = tmp_path / "snapshot.json"
    storage.save_json_secure(path, {"value": 1})
    with pytest.raises(ValueError):
        storage.save_json_secure(path, {"value": float("nan")})
    assert storage.load_json(path) == {"value": 1}


def test_existing_directory_permissions_are_not_changed(tmp_path):
    tmp_path.chmod(0o755)
    storage.save_json_secure(tmp_path / "snapshot.json", {})
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755


@pytest.mark.parametrize("operation", [storage.load_json, storage.save_json_secure, storage.append_jsonl_secure])
def test_symlink_is_never_followed(tmp_path, operation):
    target = tmp_path / "target.json"
    storage.save_json_secure(target, {"keep": True})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    args = (link,) if operation is storage.load_json else (link, {})
    with pytest.raises(ConfigError):
        operation(*args)
    assert storage.load_json(target) == {"keep": True}


def test_fifo_is_rejected_without_opening_it(monkeypatch, tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path, 0o600)
    monkeypatch.setattr(storage.os, "open", lambda *a, **k: pytest.fail("FIFO must not be opened"))
    with pytest.raises(ConfigError, match="regular file"):
        storage.load_json(path)


@pytest.mark.parametrize("text", ["[]", "null", "not-json", '{"value": NaN}'])
def test_bad_json_is_a_config_error(tmp_path, text):
    path = tmp_path / "tokens.json"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ConfigError):
        storage.load_json(path)


def test_hardlinked_destination_is_rejected(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    storage.save_json_secure(first, {})
    os.link(first, second)
    with pytest.raises(ConfigError, match="shared"):
        storage.save_json_secure(second, {"replace": True})
    assert storage.load_json(first) == {}


def test_history_append_is_private_and_keeps_previous_records(tmp_path):
    path = tmp_path / "history.jsonl"
    storage.append_jsonl_secure(path, {"sequence": 1})
    storage.append_jsonl_secure(path, {"sequence": 2})
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"sequence": 1}, {"sequence": 2}]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_partial_history_write_is_rolled_back(monkeypatch, tmp_path):
    path = tmp_path / "history.jsonl"
    storage.append_jsonl_secure(path, {"sequence": 1})
    previous = path.read_bytes()
    real_write = os.write
    calls = 0

    def partial(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:3])
        raise OSError("simulated partial write")

    monkeypatch.setattr(storage.os, "write", partial)
    with pytest.raises(OSError):
        storage.append_jsonl_secure(path, {"sequence": 2})
    assert path.read_bytes() == previous


def test_unterminated_history_is_not_overwritten(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b'{"unfinished":')
    with pytest.raises(ConfigError, match="incomplete"):
        storage.append_jsonl_secure(path, {})
    assert path.read_bytes() == b'{"unfinished":'


def test_path_aliases_and_lock_path_collisions_are_rejected(tmp_path):
    token = tmp_path / "tokens.json"
    with pytest.raises(ConfigError, match="different"):
        storage.validate_paths({"token": token, "history": token}, locked=("token", "history"))
    with pytest.raises(ConfigError, match="lock file"):
        storage.validate_paths({"token": token, "history": storage.lock_path(token)}, locked=("token", "history"))


def test_locks_release_after_exceptions_without_removing_lock_inode(tmp_path):
    path = tmp_path / "tokens.json"
    with pytest.raises(ValueError):
        with storage.exclusive_locks([path]):
            with pytest.raises(BusyError):
                with storage.exclusive_locks([path]):
                    pytest.fail("lock unexpectedly acquired")
            raise ValueError("stop")
    inode = storage.lock_path(path).stat().st_ino
    with storage.exclusive_locks([path]):
        assert storage.lock_path(path).stat().st_ino == inode


def test_lock_is_held_across_processes(tmp_path):
    path = tmp_path / "tokens.json"
    script = """
import sys
from pathlib import Path
from withings_agent_sync.storage import exclusive_locks
with exclusive_locks([Path(sys.argv[1])]):
    print('held', flush=True)
    sys.stdin.readline()
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(BusyError):
            with storage.exclusive_locks([path]):
                pytest.fail("another process owns this lock")
    finally:
        child.communicate("release\n", timeout=10)
    assert child.returncode == 0
    with storage.exclusive_locks([path]):
        pass
