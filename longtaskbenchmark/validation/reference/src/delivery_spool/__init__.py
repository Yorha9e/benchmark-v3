"""Durable filesystem-backed delivery spool."""

from __future__ import annotations

import errno
import hmac
import json
import math
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_SCHEMA_VERSION = 1
_STATUSES = frozenset({"pending", "leased", "acked", "dead"})
_PUBLIC_FIELDS = frozenset(
    {
        "message_id",
        "payload",
        "status",
        "available_at",
        "sequence",
        "attempts",
        "last_error",
    }
)
_LEASE_FIELDS = frozenset({"worker_id", "lease_token", "expires_at"})
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "next_sequence", "messages"})


@dataclass(frozen=True)
class Limits:
    max_messages: int = 10000
    max_payload_bytes: int = 65536
    max_attempts: int = 5
    lease_seconds: float = 30.0
    retry_delay: float = 10.0


class SpoolError(Exception):
    """An error with a stable machine-readable spool code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {} if details is None else dict(details)


class _DuplicateKey(ValueError):
    """Raised when a JSON object repeats a key."""


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _is_number(value: object) -> bool:
    return type(value) in (int, float) and (
        type(value) is int or math.isfinite(value)
    )


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not value.isspace()


def _canonical_json_bytes(value: object, *, trailing_newline: bool) -> bytes:
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if trailing_newline:
        text += "\n"
    return text.encode("utf-8")


def _validate_json_value(value: object, active: set[int] | None = None) -> None:
    value_type = type(value)
    if value is None or value_type in (bool, int, str):
        return
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return

    if active is None:
        active = set()
    if value_type is list:
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic payload")
        active.add(identity)
        try:
            for item in value:
                _validate_json_value(item, active)
        finally:
            active.remove(identity)
        return
    if value_type is dict:
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic payload")
        active.add(identity)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                _validate_json_value(item, active)
        finally:
            active.remove(identity)
        return
    raise ValueError("payload contains a non-JSON value")


class Spool:
    """A durable, process-safe spool stored in one canonical JSON file."""

    def __init__(self, root, *, clock, limits, lock_timeout, failpoint):
        try:
            self._root = Path(root)
        except (TypeError, ValueError, OSError) as exc:
            raise SpoolError(
                "INVALID_INPUT", "root must be a valid directory path", {"field": "root"}
            ) from exc

        if not callable(clock):
            raise SpoolError(
                "INVALID_INPUT", "clock must be callable", {"field": "clock"}
            )
        if not isinstance(limits, Limits):
            raise SpoolError(
                "INVALID_INPUT", "limits must be a Limits instance", {"field": "limits"}
            )
        self._validate_limits(limits)
        if not _is_number(lock_timeout) or lock_timeout < 0:
            raise SpoolError(
                "INVALID_INPUT",
                "lock_timeout must be a finite non-negative number",
                {"field": "lock_timeout"},
            )
        if failpoint is not None and not callable(failpoint):
            raise SpoolError(
                "INVALID_INPUT",
                "failpoint must be callable or None",
                {"field": "failpoint"},
            )

        self._clock = clock
        self._limits = limits
        self._lock_timeout = float(lock_timeout)
        self._failpoint = failpoint
        self._state_path = self._root / "state.json"
        self._lock_path = self._root / "state.lock"

    @staticmethod
    def _validate_limits(limits: Limits) -> None:
        for field in ("max_messages", "max_payload_bytes", "max_attempts"):
            value = getattr(limits, field)
            if type(value) is not int or value <= 0:
                raise SpoolError(
                    "INVALID_INPUT",
                    f"{field} must be a positive integer",
                    {"field": field},
                )
        if not _is_number(limits.lease_seconds) or limits.lease_seconds <= 0:
            raise SpoolError(
                "INVALID_INPUT",
                "lease_seconds must be a finite positive number",
                {"field": "lease_seconds"},
            )
        if not _is_number(limits.retry_delay) or limits.retry_delay < 0:
            raise SpoolError(
                "INVALID_INPUT",
                "retry_delay must be a finite non-negative number",
                {"field": "retry_delay"},
            )

    @staticmethod
    def _require_identifier(value: object, field: str) -> str:
        if not _valid_identifier(value):
            raise SpoolError(
                "INVALID_INPUT",
                f"{field} must be a non-empty, non-whitespace string",
                {"field": field},
            )
        return value

    @staticmethod
    def _require_number(value: object, field: str) -> int | float:
        if not _is_number(value):
            raise SpoolError(
                "INVALID_INPUT",
                f"{field} must be a finite number",
                {"field": field},
            )
        return value

    def _read_clock(self) -> int | float:
        try:
            value = self._clock()
        except Exception as exc:
            raise SpoolError(
                "INVALID_DEPENDENCY", "clock failed", {"dependency": "clock"}
            ) from exc
        if not _is_number(value):
            raise SpoolError(
                "INVALID_DEPENDENCY",
                "clock must return a finite number",
                {"dependency": "clock"},
            )
        return value

    @staticmethod
    def _timestamp_sum(
        left: int | float, right: int | float, *, dependency: str
    ) -> int | float:
        try:
            value = left + right
        except (OverflowError, TypeError) as exc:
            raise SpoolError(
                "INVALID_DEPENDENCY",
                "clock value cannot produce a finite timestamp",
                {"dependency": dependency},
            ) from exc
        if not _is_number(value):
            raise SpoolError(
                "INVALID_DEPENDENCY",
                "clock value cannot produce a finite timestamp",
                {"dependency": dependency},
            )
        return value

    def _canonical_payload(self, payload: object) -> tuple[object, bytes]:
        try:
            _validate_json_value(payload)
            encoded = _canonical_json_bytes(payload, trailing_newline=False)
            detached = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise SpoolError(
                "INVALID_INPUT",
                "payload must contain only finite JSON values",
                {"field": "payload"},
            ) from exc
        if len(encoded) > self._limits.max_payload_bytes:
            raise SpoolError(
                "INVALID_INPUT",
                "payload exceeds max_payload_bytes",
                {
                    "max_payload_bytes": self._limits.max_payload_bytes,
                    "payload_bytes": len(encoded),
                },
            )
        return detached, encoded

    def _call_failpoint(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)

    def _ensure_root_for_initialize(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SpoolError(
                "INTERNAL_ERROR", "cannot create spool root", {"operation": "mkdir"}
            ) from exc
        if not self._root.is_dir():
            raise SpoolError(
                "INTERNAL_ERROR", "spool root is not a directory", {"operation": "mkdir"}
            )

    def _require_existing_root(self) -> None:
        try:
            exists = self._root.exists()
            is_directory = self._root.is_dir() if exists else False
        except OSError as exc:
            raise SpoolError(
                "INTERNAL_ERROR", "cannot inspect spool root", {"operation": "stat"}
            ) from exc
        if not exists:
            raise SpoolError("NOT_INITIALIZED", "spool is not initialized")
        if not is_directory:
            raise SpoolError(
                "INTERNAL_ERROR", "spool root is not a directory", {"operation": "stat"}
            )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            lock_file = os.fdopen(descriptor, "r+b", buffering=0)
        except OSError as exc:
            raise SpoolError(
                "INTERNAL_ERROR", "cannot open spool lock", {"operation": "lock_open"}
            ) from exc

        acquired = False
        try:
            if os.name == "nt":
                try:
                    lock_file.seek(0, os.SEEK_END)
                    if lock_file.tell() == 0:
                        lock_file.write(b"\0")
                    lock_file.seek(0)
                except OSError as exc:
                    raise SpoolError(
                        "INTERNAL_ERROR",
                        "cannot prepare spool lock",
                        {"operation": "lock_prepare"},
                    ) from exc

            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    self._try_lock(lock_file)
                    acquired = True
                    break
                except OSError as exc:
                    if not self._lock_is_busy(exc):
                        raise SpoolError(
                            "INTERNAL_ERROR",
                            "cannot acquire spool lock",
                            {"operation": "lock"},
                        ) from exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SpoolError(
                            "LOCK_TIMEOUT",
                            "timed out acquiring spool lock",
                            {"timeout": self._lock_timeout},
                        ) from exc
                    time.sleep(min(0.01, remaining))

            self._call_failpoint("after_lock")
            yield
        finally:
            if acquired:
                try:
                    self._unlock(lock_file)
                except OSError:
                    acquired = False
            lock_file.close()

    @staticmethod
    def _try_lock(lock_file) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(lock_file) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _lock_is_busy(exc: OSError) -> bool:
        return exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            getattr(errno, "EDEADLK", errno.EACCES),
        }

    def _read_state(self) -> dict[str, object]:
        try:
            raw = self._state_path.read_bytes()
        except FileNotFoundError as exc:
            raise SpoolError("NOT_INITIALIZED", "spool is not initialized") from exc
        except OSError as exc:
            raise SpoolError(
                "INTERNAL_ERROR", "cannot read spool state", {"operation": "read"}
            ) from exc

        try:
            text = raw.decode("utf-8")
            state = json.loads(
                text,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_json_constant,
            )
        except (_DuplicateKey, ValueError, UnicodeError, RecursionError) as exc:
            raise SpoolError("CORRUPT_STATE", "spool state is not valid JSON") from exc

        self._validate_state(state)
        try:
            canonical = _canonical_json_bytes(state, trailing_newline=True)
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise SpoolError("CORRUPT_STATE", "spool state is not canonical") from exc
        if raw != canonical:
            raise SpoolError("CORRUPT_STATE", "spool state is not canonical")
        return state

    @staticmethod
    def _corrupt(message: str) -> SpoolError:
        return SpoolError("CORRUPT_STATE", message)

    def _validate_state(self, state: object) -> None:
        if type(state) is not dict:
            raise self._corrupt("spool state must be an object")
        if frozenset(state) != _TOP_LEVEL_FIELDS:
            raise self._corrupt("spool state has unknown or missing fields")

        schema_version = state["schema_version"]
        if type(schema_version) is not int:
            raise self._corrupt("schema_version must be an integer")
        if schema_version != _SCHEMA_VERSION:
            raise SpoolError(
                "SCHEMA_MISMATCH",
                "unsupported spool schema version",
                {"schema_version": schema_version},
            )

        next_sequence = state["next_sequence"]
        if type(next_sequence) is not int or next_sequence <= 0:
            raise self._corrupt("next_sequence must be a positive integer")
        messages = state["messages"]
        if type(messages) is not dict:
            raise self._corrupt("messages must be an object")

        sequences: set[int] = set()
        for message_id, message in messages.items():
            if type(message_id) is not str or not _valid_identifier(message_id):
                raise self._corrupt("message map keys must be valid message IDs")
            self._validate_stored_message(message_id, message)
            sequence = message["sequence"]
            if sequence in sequences:
                raise self._corrupt("message sequences must be unique")
            sequences.add(sequence)

        if sequences != set(range(1, next_sequence)):
            raise self._corrupt("next_sequence is inconsistent with messages")

    def _validate_stored_message(self, key: str, message: object) -> None:
        if type(message) is not dict:
            raise self._corrupt("stored messages must be objects")
        status = message.get("status")
        if type(status) is not str or status not in _STATUSES:
            raise self._corrupt("stored message has an invalid status")
        expected_fields = _PUBLIC_FIELDS | (_LEASE_FIELDS if status == "leased" else set())
        if frozenset(message) != expected_fields:
            raise self._corrupt("stored message has unknown or missing fields")
        if message["message_id"] != key or not _valid_identifier(message["message_id"]):
            raise self._corrupt("stored message_id is inconsistent")
        if not _is_number(message["available_at"]):
            raise self._corrupt("stored available_at must be finite")
        if type(message["sequence"]) is not int or message["sequence"] <= 0:
            raise self._corrupt("stored sequence must be a positive integer")
        if type(message["attempts"]) is not int or message["attempts"] < 0:
            raise self._corrupt("stored attempts must be a non-negative integer")
        if status in {"leased", "acked", "dead"} and message["attempts"] < 1:
            raise self._corrupt("stored status is inconsistent with attempts")
        last_error = message["last_error"]
        if last_error is not None and (type(last_error) is not str or not last_error):
            raise self._corrupt("stored last_error must be null or a non-empty string")
        try:
            _validate_json_value(message["payload"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise self._corrupt("stored payload is not valid JSON") from exc

        if status == "leased":
            if not _valid_identifier(message["worker_id"]):
                raise self._corrupt("leased message has an invalid worker_id")
            if type(message["lease_token"]) is not str or not message["lease_token"]:
                raise self._corrupt("leased message has an invalid lease_token")
            if not _is_number(message["expires_at"]):
                raise self._corrupt("leased message has an invalid expires_at")

    def _write_state(self, state: dict[str, object]) -> None:
        self._validate_state(state)
        try:
            encoded = _canonical_json_bytes(state, trailing_newline=True)
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise SpoolError(
                "INTERNAL_ERROR", "cannot serialize spool state", {"operation": "serialize"}
            ) from exc

        temporary = self._root / (
            f".state.{os.getpid()}.{secrets.token_hex(16)}.tmp"
        )
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = None
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise SpoolError(
                    "INTERNAL_ERROR",
                    "cannot write spool state",
                    {"operation": "write"},
                ) from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)

            self._call_failpoint("before_replace")
            try:
                os.replace(temporary, self._state_path)
            except OSError as exc:
                raise SpoolError(
                    "INTERNAL_ERROR",
                    "cannot replace spool state",
                    {"operation": "replace"},
                ) from exc
            self._call_failpoint("after_replace")
            self._fsync_root()
        finally:
            self._remove_temporary(temporary)

    @staticmethod
    def _remove_temporary(temporary: Path) -> None:
        try:
            temporary.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return

    def _fsync_root(self) -> None:
        if os.name == "nt":
            return
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOSYS", errno.EINVAL),
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(self._root, flags)
        except OSError as exc:
            if exc.errno in unsupported:
                return
            raise SpoolError(
                "INTERNAL_ERROR",
                "cannot open spool root for synchronization",
                {"operation": "directory_fsync"},
            ) from exc
        try:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno not in unsupported:
                    raise SpoolError(
                        "INTERNAL_ERROR",
                        "cannot synchronize spool root",
                        {"operation": "directory_fsync"},
                    ) from exc
        finally:
            os.close(descriptor)

    @staticmethod
    def _public_message(message: dict[str, object]) -> dict[str, object]:
        payload = json.loads(
            _canonical_json_bytes(
                message["payload"], trailing_newline=False
            ).decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
        return {
            "message_id": message["message_id"],
            "payload": payload,
            "status": message["status"],
            "available_at": message["available_at"],
            "sequence": message["sequence"],
            "attempts": message["attempts"],
            "last_error": message["last_error"],
        }

    @staticmethod
    def _messages(state: dict[str, object]) -> dict[str, dict[str, object]]:
        return state["messages"]

    def initialize(self) -> dict:
        self._ensure_root_for_initialize()
        with self._locked():
            if self._state_path.exists():
                self._read_state()
                raise SpoolError("ALREADY_INITIALIZED", "spool is already initialized")
            state: dict[str, object] = {
                "schema_version": _SCHEMA_VERSION,
                "next_sequence": 1,
                "messages": {},
            }
            self._write_state(state)
            return {
                "schema_version": _SCHEMA_VERSION,
                "next_sequence": 1,
                "messages": {},
            }

    def enqueue(self, message_id: str, payload, available_at=None) -> dict:
        message_id = self._require_identifier(message_id, "message_id")
        detached_payload, payload_bytes = self._canonical_payload(payload)
        explicit_available_at = available_at is not None
        if explicit_available_at:
            available_at = self._require_number(available_at, "available_at")

        self._require_existing_root()
        with self._locked():
            state = self._read_state()
            messages = self._messages(state)
            existing = messages.get(message_id)
            if existing is not None:
                existing_payload = _canonical_json_bytes(
                    existing["payload"], trailing_newline=False
                )
                same_time = (
                    not explicit_available_at
                    or existing["available_at"] == available_at
                )
                if existing_payload == payload_bytes and same_time:
                    return self._public_message(existing)
                raise SpoolError(
                    "IDEMPOTENCY_CONFLICT",
                    "message_id was already used for a different request",
                    {"message_id": message_id},
                )

            if len(messages) >= self._limits.max_messages:
                raise SpoolError(
                    "CAPACITY_EXCEEDED",
                    "spool has reached max_messages",
                    {"max_messages": self._limits.max_messages},
                )
            if not explicit_available_at:
                available_at = self._read_clock()

            sequence = state["next_sequence"]
            message = {
                "message_id": message_id,
                "payload": detached_payload,
                "status": "pending",
                "available_at": available_at,
                "sequence": sequence,
                "attempts": 0,
                "last_error": None,
            }
            messages[message_id] = message
            state["next_sequence"] = sequence + 1
            self._write_state(state)
            return self._public_message(message)

    def claim(self, worker_id: str) -> dict | None:
        worker_id = self._require_identifier(worker_id, "worker_id")
        self._require_existing_root()
        with self._locked():
            state = self._read_state()
            now = self._read_clock()
            candidates = [
                message
                for message in self._messages(state).values()
                if message["status"] == "pending" and message["available_at"] <= now
            ]
            if not candidates:
                return None
            message = min(
                candidates,
                key=lambda item: (
                    item["available_at"],
                    item["sequence"],
                    item["message_id"],
                ),
            )
            expires_at = self._timestamp_sum(
                now, self._limits.lease_seconds, dependency="clock"
            )
            try:
                lease_token = secrets.token_urlsafe(32)
            except Exception as exc:
                raise SpoolError(
                    "INTERNAL_ERROR",
                    "cannot generate lease token",
                    {"operation": "token"},
                ) from exc
            message["status"] = "leased"
            message["attempts"] += 1
            message["worker_id"] = worker_id
            message["lease_token"] = lease_token
            message["expires_at"] = expires_at
            self._write_state(state)
            result = self._public_message(message)
            result["lease_token"] = lease_token
            return result

    def _leased_message(
        self,
        state: dict[str, object],
        message_id: str,
        lease_token: str,
    ) -> dict[str, object]:
        messages = self._messages(state)
        message = messages.get(message_id)
        if message is None:
            raise SpoolError(
                "NOT_FOUND", "message was not found", {"message_id": message_id}
            )
        if message["status"] != "leased":
            raise SpoolError(
                "INVALID_STATE",
                "message is not currently leased",
                {"message_id": message_id, "status": message["status"]},
            )
        if not hmac.compare_digest(
            message["lease_token"].encode("utf-8"), lease_token.encode("utf-8")
        ):
            raise SpoolError(
                "UNAUTHORIZED",
                "lease token is not authorized",
                {"message_id": message_id},
            )
        return message

    def ack(self, message_id: str, lease_token: str) -> dict:
        message_id = self._require_identifier(message_id, "message_id")
        if type(lease_token) is not str:
            raise SpoolError(
                "INVALID_INPUT",
                "lease_token must be a string",
                {"field": "lease_token"},
            )
        self._require_existing_root()
        with self._locked():
            state = self._read_state()
            message = self._leased_message(state, message_id, lease_token)
            message["status"] = "acked"
            for field in _LEASE_FIELDS:
                message.pop(field, None)
            self._write_state(state)
            return self._public_message(message)

    def fail(self, message_id: str, lease_token: str, error: str) -> dict:
        message_id = self._require_identifier(message_id, "message_id")
        if type(lease_token) is not str:
            raise SpoolError(
                "INVALID_INPUT",
                "lease_token must be a string",
                {"field": "lease_token"},
            )
        if type(error) is not str or not error:
            raise SpoolError(
                "INVALID_INPUT",
                "error must be a non-empty string",
                {"field": "error"},
            )
        self._require_existing_root()
        with self._locked():
            state = self._read_state()
            message = self._leased_message(state, message_id, lease_token)
            message["last_error"] = error
            for field in _LEASE_FIELDS:
                message.pop(field, None)
            if message["attempts"] >= self._limits.max_attempts:
                message["status"] = "dead"
            else:
                now = self._read_clock()
                message["status"] = "pending"
                message["available_at"] = self._timestamp_sum(
                    now, self._limits.retry_delay, dependency="clock"
                )
            self._write_state(state)
            return self._public_message(message)

    def recover(self) -> dict:
        self._require_existing_root()
        with self._locked():
            state = self._read_state()
            now = self._read_clock()
            recovered = 0
            for message in self._messages(state).values():
                if message["status"] != "leased" or now < message["expires_at"]:
                    continue
                for field in _LEASE_FIELDS:
                    message.pop(field, None)
                if message["attempts"] >= self._limits.max_attempts:
                    message["status"] = "dead"
                else:
                    message["status"] = "pending"
                    message["available_at"] = now
                recovered += 1
            if recovered:
                self._write_state(state)
            return {"recovered": recovered}

    def get(self, message_id: str) -> dict:
        message_id = self._require_identifier(message_id, "message_id")
        self._require_existing_root()
        with self._locked():
            state = self._read_state()
            message = self._messages(state).get(message_id)
            if message is None:
                raise SpoolError(
                    "NOT_FOUND", "message was not found", {"message_id": message_id}
                )
            return self._public_message(message)

    def list_messages(self, status=None) -> list[dict]:
        if status is not None and (type(status) is not str or status not in _STATUSES):
            raise SpoolError(
                "INVALID_INPUT",
                "status must be one of pending, leased, acked, or dead",
                {"field": "status"},
            )
        self._require_existing_root()
        with self._locked():
            state = self._read_state()
            selected = [
                message
                for message in self._messages(state).values()
                if status is None or message["status"] == status
            ]
            selected.sort(
                key=lambda item: (
                    item["available_at"],
                    item["sequence"],
                    item["message_id"],
                )
            )
            return [self._public_message(message) for message in selected]


__all__ = ["Limits", "Spool", "SpoolError"]
