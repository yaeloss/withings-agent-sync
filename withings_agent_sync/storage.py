"""Private POSIX files, atomic snapshots, and non-blocking process locks."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import BusyError, ConfigError


def _require_posix() -> None:
    if os.name != "posix":
        raise ConfigError("Private file storage currently supports Linux and macOS only.")


def _check_regular(info: os.stat_result, path: Path, *, writable: bool = False) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"Expected a regular file, not a symlink or directory: {path}")
    if writable and (info.st_nlink != 1 or info.st_uid != os.geteuid()):
        raise ConfigError(f"Refusing to write a shared or differently owned file: {path}")


def _check_destination(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    _check_regular(info, path, writable=True)


def _private_parent(path: Path) -> None:
    # Never chmod existing ancestors: --history might point into /tmp or a
    # directory shared with other applications. New leaf directories are private.
    if path.parent.is_symlink():
        raise ConfigError(f"Refusing to write through a symlinked directory: {path.parent}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def _open_flags(flags: int) -> int:
    return flags | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_json(path: Path | str) -> dict[str, Any]:
    _require_posix()
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ConfigError(f"Refusing to read a symlink: {path}")
    try:
        _check_regular(path.lstat(), path)
        fd = os.open(path, _open_flags(os.O_RDONLY))
        try:
            info = os.fstat(fd)
            _check_regular(info, path)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ConfigError(f"Refusing to read a non-private file: {path} (expected mode 0600)")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                payload = json.load(handle, parse_constant=_reject_json_constant)
        finally:
            if fd != -1:
                os.close(fd)
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"Could not read JSON file {path}. Check the path, permissions, and UTF-8 JSON syntax."
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"Expected a JSON object in {path}.")
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def save_json_secure(path: Path | str, payload: dict[str, Any]) -> None:
    """Replace a JSON file atomically; secrets never appear in a public tempfile."""
    _require_posix()
    path = Path(path).expanduser()
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    _check_destination(path)
    _private_parent(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _check_destination(path)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def check_history(path: Path | str) -> None:
    """Do not append a new record onto an interrupted, unterminated line."""
    path = Path(path).expanduser()
    _check_destination(path)
    if path.exists():
        fd = os.open(path, _open_flags(os.O_RDONLY))
        try:
            info = os.fstat(fd)
            _check_regular(info, path, writable=True)
            if info.st_size and os.pread(fd, 1, info.st_size - 1) != b"\n":
                raise ConfigError(f"History has an incomplete final line; repair it before syncing: {path}")
        finally:
            os.close(fd)


def append_jsonl_secure(path: Path | str, payload: dict[str, Any]) -> None:
    """Append one private, durable record while the caller holds its file lock."""
    _require_posix()
    path = Path(path).expanduser()
    data = (json.dumps(payload, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    _check_destination(path)
    _private_parent(path)
    fd = os.open(path, _open_flags(os.O_CREAT | os.O_RDWR | os.O_APPEND), 0o600)
    try:
        info = os.fstat(fd)
        _check_regular(info, path, writable=True)
        os.fchmod(fd, 0o600)
        previous_size = info.st_size
        if previous_size and os.pread(fd, 1, previous_size - 1) != b"\n":
            raise ConfigError(f"History has an incomplete final line; repair it before syncing: {path}")
        try:
            written = 0
            while written < len(data):
                count = os.write(fd, data[written:])
                if not count:
                    raise OSError("History write made no progress")
                written += count
            os.fsync(fd)
        except OSError:
            os.ftruncate(fd, previous_size)
            os.fsync(fd)
            raise
    finally:
        os.close(fd)
    _fsync_directory(path.parent)


def lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def validate_paths(paths: dict[str, Path], *, locked: tuple[str, ...]) -> None:
    """Reject aliases, hardlinks, and application files that overlap lock files."""
    resolved: dict[str, Path] = {}
    for label, path in paths.items():
        if path.is_symlink():
            raise ConfigError(f"{label} must not be a symlink: {path}")
        if label in locked and path.parent.is_symlink():
            raise ConfigError(f"{label} must not use a symlinked directory: {path.parent}")
        if path.exists():
            _check_regular(path.stat(), path, writable=label in locked)
        canonical = path.resolve()
        for other, previous in resolved.items():
            if canonical == previous or (canonical.exists() and previous.exists() and canonical.samefile(previous)):
                raise ConfigError(f"Paths for {label} and {other} must be different.")
        resolved[label] = canonical
    locks = {lock_path(resolved[label]) for label in locked}
    for label, path in resolved.items():
        if path in locks:
            raise ConfigError(f"Path for {label} conflicts with an application lock file.")


@contextmanager
def exclusive_locks(paths: list[Path]) -> Iterator[None]:
    """Use persistent lock inodes; unlinking them would let concurrent runs bypass locks."""
    _require_posix()
    import fcntl

    handles: list[int] = []
    try:
        for path in sorted({path.resolve() for path in paths}, key=str):
            lock = lock_path(path)
            _check_destination(lock)
            _private_parent(lock)
            fd = os.open(lock, _open_flags(os.O_CREAT | os.O_RDWR), 0o600)
            handles.append(fd)
            _check_regular(os.fstat(fd), lock, writable=True)
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusyError(f"Another process is using {path}. Wait for it to finish and retry.") from exc
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)
