"""Exclusive, conservatively recoverable process lock for unified radar runs.

A permanent mode-0600 sidecar gate serializes short lock-management operations.
The gate is never removed: ``flock`` is crash-released while the canonical JSON
lock continues to represent ownership of the user critical section.
Absolute lock paths must use canonical, non-symlink directory components; the
descriptor walk deliberately does not resolve symlink-prefixed aliases.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Callable, Dict, Optional, Tuple
import weakref

from .errors import InputValidationError, PersistenceError, RunBusyError


_RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8,32}\Z")
_LOCK_FIELDS = {"pid", "run_id", "host", "acquired_at"}
_MAX_LOCK_BYTES = 64 * 1024
_STALE_AFTER = timedelta(hours=2)
_MAX_ACQUIRE_ATTEMPTS = 4
_GATE_NAME = ".unified-game-radar-run-lock.gate"


class _MissingLock(Exception):
    """Internal signal that a racing owner removed the lock."""


class _UnsafeExistingLock(Exception):
    """Internal signal that an existing lock must be left untouched."""


class _QuarantineFailure(Exception):
    """Internal signal that atomic ownership removal was not provable."""


LockPayload = Dict[str, object]
LockIdentity = Tuple[int, int, int, int]


def _close_fd_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _validate_run_id(value: object) -> str:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise InputValidationError(
            "run_id must use YYYYMMDDTHHMMSSZ followed by 8 to 32 lowercase hex characters"
        )
    timestamp = value[:16]
    try:
        parsed = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ")
    except (TypeError, ValueError) as exc:
        raise InputValidationError("run_id contains an invalid UTC timestamp") from exc
    if parsed.strftime("%Y%m%dT%H%M%SZ") != timestamp:
        raise InputValidationError("run_id contains an invalid UTC timestamp")
    return value


def _validate_host(value: object, *, require_trimmed: bool = False) -> str:
    if not isinstance(value, str):
        raise InputValidationError("hostname callback must return a string")
    trimmed = value.strip()
    if not trimmed or (require_trimmed and trimmed != value):
        raise InputValidationError("hostname must be a nonempty trimmed string")
    if "/" in trimmed or "\\" in trimmed:
        raise InputValidationError("hostname must not contain path separators")
    if any(ord(character) < 32 or ord(character) == 127 for character in trimmed):
        raise InputValidationError("hostname must not contain control characters")
    try:
        trimmed.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise InputValidationError("hostname must be valid UTF-8") from exc
    return trimmed


def _utc_second(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise InputValidationError("now callback must return a datetime")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise InputValidationError("now callback must return an aware datetime")
        return value.astimezone(timezone.utc).replace(microsecond=0)
    except InputValidationError:
        raise
    except Exception as exc:
        raise InputValidationError("now callback returned an invalid datetime") from exc


def _format_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_second(value: object) -> datetime:
    if not isinstance(value, str):
        raise _UnsafeExistingLock
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise _UnsafeExistingLock from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise _UnsafeExistingLock
    return parsed.replace(tzinfo=timezone.utc)


def _identity(metadata: os.stat_result) -> LockIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _same_open_file(before: os.stat_result, after: os.stat_result) -> bool:
    return _identity(before) == _identity(after) and stat.S_ISREG(after.st_mode)


def _strict_json(data: bytes) -> object:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _UnsafeExistingLock from exc

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _UnsafeExistingLock
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                _UnsafeExistingLock()
            ),
        )
    except _UnsafeExistingLock:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _UnsafeExistingLock from exc


def _validate_existing_payload(value: object) -> LockPayload:
    if not isinstance(value, dict) or set(value) != _LOCK_FIELDS:
        raise _UnsafeExistingLock
    pid = value.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise _UnsafeExistingLock
    try:
        _validate_run_id(value.get("run_id"))
        _validate_host(value.get("host"), require_trimmed=True)
    except InputValidationError as exc:
        raise _UnsafeExistingLock from exc
    _parse_utc_second(value.get("acquired_at"))
    return value


class RunLock:
    """Own an exclusive JSON lock file for the lifetime of a context."""

    def __init__(
        self,
        path: Path,
        run_id: str,
        now: Callable[[], datetime],
        hostname: Callable[[], str],
        pid_alive: Callable[[int], bool],
    ) -> None:
        if not isinstance(path, Path):
            raise InputValidationError("path must be a pathlib.Path")
        if not path.name or path.name in {".", ".."}:
            raise InputValidationError("path must identify a lock file")
        if ".." in path.parts:
            raise InputValidationError("lock path must not traverse parent directories")
        self.path = path
        self.run_id = _validate_run_id(run_id)
        callbacks = (("now", now), ("hostname", hostname), ("pid_alive", pid_alive))
        for name, callback in callbacks:
            if not callable(callback):
                raise InputValidationError(f"{name} must be callable")
        self._now = now
        self._hostname = hostname
        self._pid_alive = pid_alive
        self._acquired = False
        self._payload: Optional[LockPayload] = None
        self._created_identity: Optional[LockIdentity] = None
        self._parent_descriptor: Optional[int] = None
        self._parent_finalizer: Optional[weakref.finalize] = None

    def _callback_context(self) -> tuple[datetime, str, int]:
        try:
            current = self._now()
        except Exception as exc:
            raise InputValidationError("now callback failed") from exc
        now_utc = _utc_second(current)
        try:
            host_value = self._hostname()
        except Exception as exc:
            raise InputValidationError("hostname callback failed") from exc
        host = _validate_host(host_value)
        pid = os.getpid()
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise InputValidationError("process PID must be a positive integer")
        return now_utc, host, pid

    def _open_parent(self) -> int:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            if self.path.is_absolute():
                descriptor = os.open(os.path.sep, flags)
                components = self.path.parent.parts[1:]
            else:
                descriptor = os.open(".", flags)
                components = self.path.parent.parts
        except OSError as exc:
            raise PersistenceError("cannot open trusted lock-path root") from exc

        try:
            for component in components:
                if component in {"", "."}:
                    continue
                if component == "..":
                    raise InputValidationError(
                        "lock path must not traverse parent directories"
                    )
                try:
                    next_descriptor = os.open(
                        component,
                        flags,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise PersistenceError(
                            f"cannot create lock directory component: {component}"
                        ) from exc
                    try:
                        next_descriptor = os.open(
                            component,
                            flags,
                            dir_fd=descriptor,
                        )
                    except OSError as exc:
                        raise PersistenceError(
                            f"cannot securely open lock directory: {component}"
                        ) from exc
                except OSError as exc:
                    raise PersistenceError(
                        f"cannot securely open lock directory: {component}"
                    ) from exc
                try:
                    if not stat.S_ISDIR(os.fstat(next_descriptor).st_mode):
                        raise PersistenceError(
                            f"lock path component is not a directory: {component}"
                        )
                except Exception:
                    try:
                        os.close(next_descriptor)
                    except OSError:
                        pass
                    raise
                old_descriptor = descriptor
                try:
                    os.close(old_descriptor)
                except OSError as exc:
                    try:
                        os.close(next_descriptor)
                    except OSError:
                        pass
                    descriptor = -1
                    raise PersistenceError(
                        "cannot close traversed lock-directory descriptor"
                    ) from exc
                descriptor = next_descriptor
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise PersistenceError("lock parent is not a directory")
        except Exception as exc:
            try:
                os.close(descriptor)
            except OSError:
                pass
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("cannot securely traverse lock parent") from exc
        return descriptor

    def _validate_gate_metadata(
        self,
        metadata: os.stat_result,
        *,
        require_exact_mode: bool,
    ) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise PersistenceError("run-lock gate is not a regular file")
        if metadata.st_nlink != 1:
            raise PersistenceError("run-lock gate must have exactly one link")
        if metadata.st_size != 0:
            raise PersistenceError("run-lock gate must be empty")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise PersistenceError("run-lock gate has an unexpected owner")
        mode = stat.S_IMODE(metadata.st_mode)
        if require_exact_mode:
            if mode != 0o600:
                raise PersistenceError("existing run-lock gate must use mode 0600")
        elif mode & ~0o600:
            raise PersistenceError("new run-lock gate has unsafe permissions")

    def _gate_name_metadata(self, parent_descriptor: int) -> os.stat_result:
        try:
            return os.stat(
                _GATE_NAME,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise PersistenceError("cannot inspect run-lock gate name") from exc

    def _acquire_gate(self, parent_descriptor: int) -> int:
        flags = os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        created = False
        try:
            descriptor = os.open(
                _GATE_NAME,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise PersistenceError("cannot securely create run-lock gate") from exc
            try:
                descriptor = os.open(
                    _GATE_NAME,
                    flags,
                    dir_fd=parent_descriptor,
                )
            except OSError as open_error:
                raise PersistenceError(
                    "cannot securely open existing run-lock gate"
                ) from open_error
        else:
            created = True
        try:
            metadata = os.fstat(descriptor)
            self._validate_gate_metadata(
                metadata,
                require_exact_mode=not created,
            )
            named_metadata = self._gate_name_metadata(parent_descriptor)
            self._validate_gate_metadata(
                named_metadata,
                require_exact_mode=not created,
            )
            if (metadata.st_dev, metadata.st_ino) != (
                named_metadata.st_dev,
                named_metadata.st_ino,
            ):
                raise PersistenceError("run-lock gate name changed during open")
            if created:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    break
                except InterruptedError:
                    continue
                except OSError as exc:
                    raise PersistenceError("cannot acquire run-lock gate") from exc
            locked_metadata = os.fstat(descriptor)
            self._validate_gate_metadata(
                locked_metadata,
                require_exact_mode=True,
            )
            locked_named_metadata = self._gate_name_metadata(parent_descriptor)
            self._validate_gate_metadata(
                locked_named_metadata,
                require_exact_mode=True,
            )
            if (locked_metadata.st_dev, locked_metadata.st_ino) != (
                locked_named_metadata.st_dev,
                locked_named_metadata.st_ino,
            ):
                raise PersistenceError("run-lock gate changed before acquisition")
            return descriptor
        except Exception as exc:
            try:
                os.close(descriptor)
            except OSError:
                pass
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("cannot prepare run-lock gate") from exc

    def _release_gate(self, descriptor: int) -> None:
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _read_named(
        self,
        parent_descriptor: int,
        name: str,
    ) -> tuple[LockPayload, LockIdentity]:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError as exc:
            raise _MissingLock from exc
        except OSError as exc:
            raise _UnsafeExistingLock from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_LOCK_BYTES:
                raise _UnsafeExistingLock
            chunks: list[bytes] = []
            remaining = _MAX_LOCK_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(data) > _MAX_LOCK_BYTES
                or len(data) != before.st_size
                or not _same_open_file(before, after)
            ):
                raise _UnsafeExistingLock
            payload = _validate_existing_payload(_strict_json(data))
            return payload, _identity(after)
        except _UnsafeExistingLock:
            raise
        except (OSError, OverflowError) as exc:
            raise _UnsafeExistingLock from exc
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _read_existing(
        self, parent_descriptor: int
    ) -> tuple[LockPayload, LockIdentity]:
        return self._read_named(parent_descriptor, self.path.name)

    def _named_identity(
        self,
        parent_descriptor: int,
        name: str,
    ) -> LockIdentity:
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise _MissingLock from exc
        except OSError as exc:
            raise _UnsafeExistingLock from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise _UnsafeExistingLock
        return _identity(metadata)

    def _fsync_parent(self, parent_descriptor: int) -> None:
        try:
            os.fsync(parent_descriptor)
        except OSError as exc:
            raise _QuarantineFailure("cannot sync lock directory") from exc

    def _unused_quarantine_name(self, parent_descriptor: int) -> str:
        for _attempt in range(_MAX_ACQUIRE_ATTEMPTS):
            token = secrets.token_hex(16)
            if re.fullmatch(r"[0-9a-f]{32}", token) is None:
                raise _QuarantineFailure("invalid quarantine token")
            name = f".unified-game-radar-lock-{token}.quarantine"
            try:
                os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return name
            except OSError as exc:
                raise _QuarantineFailure(
                    "cannot inspect quarantine path"
                ) from exc
        raise _QuarantineFailure("cannot allocate quarantine path")

    def _restore_quarantine(
        self,
        parent_descriptor: int,
        quarantine_name: str,
    ) -> None:
        try:
            os.link(
                quarantine_name,
                self.path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _QuarantineFailure(
                "cannot safely restore unexpected quarantined lock"
            ) from exc
        try:
            os.unlink(quarantine_name, dir_fd=parent_descriptor)
        except OSError as exc:
            raise _QuarantineFailure(
                "restored lock has an extra quarantine link"
            ) from exc
        self._fsync_parent(parent_descriptor)

    def _atomic_remove_expected(
        self,
        parent_descriptor: int,
        payload: LockPayload,
        identity: LockIdentity,
    ) -> bool:
        quarantine_name = self._unused_quarantine_name(parent_descriptor)
        try:
            os.rename(
                self.path.name,
                quarantine_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _QuarantineFailure("cannot atomically quarantine lock") from exc

        try:
            moved_payload, moved_identity = self._read_named(
                parent_descriptor,
                quarantine_name,
            )
        except (_MissingLock, _UnsafeExistingLock) as exc:
            try:
                self._restore_quarantine(parent_descriptor, quarantine_name)
            except _QuarantineFailure as restore_error:
                raise _QuarantineFailure(
                    "cannot verify or restore quarantined lock"
                ) from restore_error
            raise _QuarantineFailure("quarantined lock could not be verified") from exc

        if moved_identity != identity or moved_payload != payload:
            try:
                self._restore_quarantine(parent_descriptor, quarantine_name)
            except _QuarantineFailure as exc:
                raise _QuarantineFailure(
                    "unexpected quarantined lock could not be restored"
                ) from exc
            raise _QuarantineFailure(
                "lock ownership changed before atomic quarantine"
            )

        try:
            os.unlink(quarantine_name, dir_fd=parent_descriptor)
        except OSError as exc:
            raise _QuarantineFailure(
                "cannot delete verified quarantined lock"
            ) from exc
        self._fsync_parent(parent_descriptor)
        return True

    def _atomic_remove_created_inode(
        self,
        parent_descriptor: int,
        identity: LockIdentity,
    ) -> bool:
        """Remove a partially written lock only if its open-file inode moved."""
        quarantine_name = self._unused_quarantine_name(parent_descriptor)
        try:
            os.rename(
                self.path.name,
                quarantine_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _QuarantineFailure(
                "cannot quarantine partially created lock"
            ) from exc
        try:
            moved_identity = self._named_identity(
                parent_descriptor,
                quarantine_name,
            )
        except (_MissingLock, _UnsafeExistingLock) as exc:
            try:
                self._restore_quarantine(parent_descriptor, quarantine_name)
            except _QuarantineFailure as restore_error:
                raise _QuarantineFailure(
                    "cannot inspect or restore partial lock"
                ) from restore_error
            raise _QuarantineFailure("partial lock could not be inspected") from exc
        if moved_identity[:2] != identity[:2]:
            try:
                self._restore_quarantine(parent_descriptor, quarantine_name)
            except _QuarantineFailure as exc:
                raise _QuarantineFailure(
                    "replacement lock could not be restored"
                ) from exc
            raise _QuarantineFailure("partial lock inode was replaced")
        try:
            os.unlink(quarantine_name, dir_fd=parent_descriptor)
        except OSError as exc:
            raise _QuarantineFailure(
                "cannot delete quarantined partial lock"
            ) from exc
        self._fsync_parent(parent_descriptor)
        return True

    def _remove_stale(
        self,
        parent_descriptor: int,
        payload: LockPayload,
        identity: LockIdentity,
    ) -> bool:
        try:
            return self._atomic_remove_expected(
                parent_descriptor,
                payload,
                identity,
            )
        except _QuarantineFailure as exc:
            raise RunBusyError(
                "stale run lock could not be removed safely"
            ) from exc

    def _collision_is_removable(
        self,
        payload: LockPayload,
        *,
        now_utc: datetime,
        host: str,
    ) -> bool:
        acquired_at = _parse_utc_second(payload["acquired_at"])
        age = now_utc - acquired_at
        if age.total_seconds() < 0:
            return False
        if payload["host"] != host or age <= _STALE_AFTER:
            return False
        try:
            alive = self._pid_alive(payload["pid"])
        except Exception:
            return False
        if not isinstance(alive, bool):
            return False
        return not alive

    def _write_new(
        self,
        parent_descriptor: int,
        payload: LockPayload,
    ) -> Optional[LockIdentity]:
        encoded = self._encode_payload(payload)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(
                self.path.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ELOOP, errno.EISDIR}:
                return None
            raise PersistenceError("cannot create run lock") from exc

        created_identity: Optional[LockIdentity] = None
        try:
            created_identity = _identity(os.fstat(descriptor))
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short lock write")
                view = view[written:]
            os.fsync(descriptor)
            final_metadata = os.fstat(descriptor)
            final_identity = _identity(final_metadata)
            if final_identity[:2] != created_identity[:2]:
                raise OSError("lock identity changed during creation")
            return final_identity
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            if created_identity is not None:
                try:
                    self._atomic_remove_created_inode(
                        parent_descriptor,
                        created_identity,
                    )
                except _QuarantineFailure:
                    pass
            raise PersistenceError("cannot persist run lock") from exc
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _encode_payload(self, payload: LockPayload) -> bytes:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise InputValidationError("lock payload is not valid UTF-8 JSON") from exc
        if len(encoded) > _MAX_LOCK_BYTES:
            raise InputValidationError("lock payload exceeds 64 KiB")
        return encoded

    def acquire(self) -> "RunLock":
        """Acquire this lock and return its owner handle.

        This is the explicit equivalent of entering the context manager.
        """
        return self.__enter__()

    def release(self) -> None:
        """Release this lock if this instance still owns it.

        Releasing an unacquired or already released lock is harmless.
        """
        self.__exit__(None, None, None)

    def __enter__(self) -> "RunLock":
        if self._acquired:
            raise RunBusyError("this RunLock instance is already acquired")
        now_utc, host, pid = self._callback_context()
        payload: LockPayload = {
            "pid": pid,
            "run_id": self.run_id,
            "host": host,
            "acquired_at": _format_utc(now_utc),
        }
        self._encode_payload(payload)
        parent_descriptor = self._open_parent()
        gate_descriptor: Optional[int] = None
        retain_parent_descriptor = False
        try:
            gate_descriptor = self._acquire_gate(parent_descriptor)
            removed_stale_lock = False
            for _attempt in range(_MAX_ACQUIRE_ATTEMPTS):
                identity = self._write_new(parent_descriptor, payload)
                if identity is not None:
                    parent_finalizer = weakref.finalize(
                        self,
                        _close_fd_quietly,
                        parent_descriptor,
                    )
                    self._payload = payload
                    self._created_identity = identity
                    self._parent_descriptor = parent_descriptor
                    self._parent_finalizer = parent_finalizer
                    self._acquired = True
                    retain_parent_descriptor = True
                    return self
                if removed_stale_lock:
                    raise RunBusyError(
                        "a new owner raced stale-lock recovery"
                    )
                try:
                    existing, existing_identity = self._read_existing(
                        parent_descriptor
                    )
                except _MissingLock:
                    continue
                except _UnsafeExistingLock as exc:
                    raise RunBusyError("another run lock cannot be safely inspected") from exc
                if not self._collision_is_removable(
                    existing,
                    now_utc=now_utc,
                    host=host,
                ):
                    raise RunBusyError("another radar run owns the lock")
                if not self._remove_stale(
                    parent_descriptor,
                    existing,
                    existing_identity,
                ):
                    raise RunBusyError("run lock changed while checking staleness")
                removed_stale_lock = True
            raise RunBusyError("run lock was contended during acquisition")
        finally:
            if gate_descriptor is not None:
                self._release_gate(gate_descriptor)
            if not retain_parent_descriptor:
                try:
                    os.close(parent_descriptor)
                except OSError:
                    pass

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if not self._acquired:
            return False
        payload = self._payload
        identity = self._created_identity
        parent_descriptor = self._parent_descriptor
        parent_finalizer = self._parent_finalizer
        if (
            payload is None
            or identity is None
            or parent_descriptor is None
            or parent_finalizer is None
        ):
            if exc_type is None:
                raise PersistenceError("run-lock ownership state is incomplete")
            return False
        gate_descriptor: Optional[int] = None
        ownership_resolved = False
        try:
            try:
                gate_descriptor = self._acquire_gate(parent_descriptor)
            except PersistenceError:
                if exc_type is None:
                    raise
                return False
            try:
                current, current_identity = self._read_existing(parent_descriptor)
            except _MissingLock:
                ownership_resolved = True
                return False
            except _UnsafeExistingLock as inspection_error:
                if exc_type is None:
                    raise PersistenceError(
                        "cannot safely inspect owned run lock"
                    ) from inspection_error
                return False
            if current != payload or current_identity[:2] != identity[:2]:
                ownership_resolved = True
                return False
            try:
                self._atomic_remove_expected(
                    parent_descriptor,
                    payload,
                    current_identity,
                )
            except _QuarantineFailure as removal_error:
                if exc_type is None:
                    raise PersistenceError(
                        "cannot release owned run lock atomically"
                    ) from removal_error
                return False
            ownership_resolved = True
            return False
        finally:
            if gate_descriptor is not None:
                self._release_gate(gate_descriptor)
            if ownership_resolved:
                self._acquired = False
                self._payload = None
                self._created_identity = None
                self._parent_descriptor = None
                self._parent_finalizer = None
                parent_finalizer()
