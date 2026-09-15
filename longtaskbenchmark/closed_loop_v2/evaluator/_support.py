"""Shared black-box helpers for the closed-loop v2 evaluator."""

from __future__ import annotations

import contextlib
import gc
import json
import math
import multiprocessing
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Sequence


ORDER_KEYS = {
    "order_id",
    "idempotency_key",
    "status",
    "items",
    "total_cents",
    "payment_id",
    "created_at",
    "updated_at",
}
MESSAGE_KEYS = {
    "message_id",
    "payload",
    "status",
    "available_at",
    "sequence",
    "attempts",
    "last_error",
}


class FakeClock:
    """A manually advanced clock which also records dependency calls."""

    def __init__(self, now: object = 0.0):
        self.now = now
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return self.now

    def set(self, value: object) -> None:
        self.now = value

    def advance(self, seconds: int | float) -> None:
        self.now = self.now + seconds  # type: ignore[operator]


class DeterministicIds:
    """A deterministic, call-counting zero-argument ID factory."""

    def __init__(self, values: Sequence[object] | None = None, *, prefix: str = "order"):
        self._values = list(values) if values is not None else None
        self.prefix = prefix
        self.calls = 0

    def __call__(self) -> object:
        index = self.calls
        self.calls += 1
        if self._values is not None:
            if index >= len(self._values):
                raise AssertionError("id_factory called more often than expected")
            return self._values[index]
        return f"{self.prefix}-{index + 1}"


@contextlib.contextmanager
def temporary_workspace(prefix: str) -> Iterator[Path]:
    """Yield a temporary root and close candidate SQLite handles before Windows cleanup."""

    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        try:
            yield Path(directory)
        finally:
            for value in gc.get_objects():
                if isinstance(value, sqlite3.Connection):
                    try:
                        value.close()
                    except sqlite3.Error:
                        continue
            gc.collect()


@contextlib.contextmanager
def expect_domain_error(case: Any, error_type: type[BaseException], code: str) -> Iterator[None]:
    """Assert the complete public exception contract around a failing call."""

    with case.assertRaises(error_type) as caught:
        yield
    error = caught.exception
    case.assertEqual(error.code, code)
    case.assertIs(type(error.code), str)
    case.assertIs(type(error.message), str)
    case.assertTrue(error.message)
    case.assertEqual(str(error), error.message)
    case.assertIs(type(error.details), dict)


def assert_finite_number(case: Any, value: object) -> None:
    case.assertIn(type(value), (int, float))
    case.assertTrue(math.isfinite(value))


def assert_order_schema(case: Any, order: object, *, status: str | None = None) -> dict[str, Any]:
    case.assertIs(type(order), dict)
    value = order
    case.assertEqual(set(value), ORDER_KEYS)
    case.assertIs(type(value["order_id"]), str)
    case.assertIs(type(value["idempotency_key"]), str)
    case.assertIn(value["status"], {"pending", "paid", "shipped", "cancelled"})
    if status is not None:
        case.assertEqual(value["status"], status)
    case.assertIs(type(value["items"]), list)
    case.assertEqual(value["items"], sorted(value["items"], key=lambda item: item["sku"]))
    for item in value["items"]:
        case.assertIs(type(item), dict)
        case.assertEqual(set(item), {"sku", "quantity", "unit_price_cents"})
        case.assertIs(type(item["sku"]), str)
        case.assertIs(type(item["quantity"]), int)
        case.assertIs(type(item["unit_price_cents"]), int)
    case.assertIs(type(value["total_cents"]), int)
    case.assertTrue(value["payment_id"] is None or type(value["payment_id"]) is str)
    assert_finite_number(case, value["created_at"])
    assert_finite_number(case, value["updated_at"])
    return value


def assert_message_schema(
    case: Any,
    message: object,
    *,
    status: str | None = None,
    claimed: bool = False,
) -> dict[str, Any]:
    case.assertIs(type(message), dict)
    value = message
    expected = MESSAGE_KEYS | ({"lease_token"} if claimed else set())
    case.assertEqual(set(value), expected)
    case.assertIs(type(value["message_id"]), str)
    case.assertIn(value["status"], {"pending", "leased", "acked", "dead"})
    if status is not None:
        case.assertEqual(value["status"], status)
    assert_finite_number(case, value["available_at"])
    case.assertIs(type(value["sequence"]), int)
    case.assertIs(type(value["attempts"]), int)
    case.assertTrue(value["last_error"] is None or type(value["last_error"]) is str)
    if claimed:
        case.assertIs(type(value["lease_token"]), str)
        case.assertTrue(value["lease_token"].strip())
    return value


def candidate_workspace() -> Path:
    """Return the workspace containing the already-selected candidate src package."""

    import src

    locations = list(getattr(src, "__path__", ()))
    if not locations:
        raise AssertionError("candidate src package has no filesystem location")
    workspace = Path(locations[0]).resolve().parent
    if not (workspace / "src").is_dir():
        raise AssertionError(f"candidate workspace does not contain src: {workspace}")
    return workspace


def clean_python_environment() -> dict[str, str]:
    """Build a subprocess environment isolated from Python path/user hooks."""

    environment = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
    )
    return environment


def run_cli(
    module: str,
    option: str,
    storage_path: Path,
    command: str,
    request: object = None,
    *,
    raw_input: str | None = None,
    timeout: float = 15.0,
) -> tuple[subprocess.CompletedProcess[str], object]:
    """Run one candidate CLI command and decode its one-line JSON response."""

    if raw_input is None:
        stdin = "" if request is None else json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n"
    else:
        stdin = raw_input
    completed = subprocess.run(
        [sys.executable, "-m", module, option, str(storage_path), command],
        input=stdin,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=candidate_workspace(),
        env=clean_python_environment(),
        timeout=timeout,
        check=False,
    )
    lines = completed.stdout.splitlines()
    if len(lines) != 1 or not completed.stdout.endswith("\n"):
        raise AssertionError(f"CLI must emit exactly one newline-terminated line: {completed.stdout!r}")
    try:
        decoded = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise AssertionError(f"CLI stdout is not JSON: {completed.stdout!r}") from error
    canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
    if completed.stdout != canonical:
        raise AssertionError(f"CLI stdout is not compact canonical JSON: {completed.stdout!r}")
    return completed, decoded


def assert_cli_success(case: Any, completed: subprocess.CompletedProcess[str], decoded: object) -> object:
    case.assertEqual(completed.returncode, 0)
    case.assertEqual(completed.stderr, "")
    case.assertIs(type(decoded), dict)
    case.assertEqual(set(decoded), {"ok", "result"})
    case.assertIs(decoded["ok"], True)
    return decoded["result"]


def assert_cli_error(
    case: Any,
    completed: subprocess.CompletedProcess[str],
    decoded: object,
    *,
    exit_code: int,
    code: str,
) -> dict[str, Any]:
    case.assertEqual(completed.returncode, exit_code)
    case.assertIs(type(decoded), dict)
    case.assertEqual(set(decoded), {"ok", "error"})
    case.assertIs(decoded["ok"], False)
    error = decoded["error"]
    case.assertIs(type(error), dict)
    case.assertEqual(set(error), {"code", "message", "details"})
    case.assertEqual(error["code"], code)
    case.assertIs(type(error["message"]), str)
    case.assertTrue(error["message"])
    case.assertIs(type(error["details"]), dict)
    return error


def order_create_worker(
    db_path: str,
    idempotency_key: str,
    order_id: str,
    barrier: multiprocessing.synchronize.Barrier,
    result_queue: multiprocessing.queues.Queue,
) -> None:
    """Spawn-safe worker racing an order reservation against shared SQLite."""

    try:
        from src.order_fulfillment import OrderError, OrderService

        clock = FakeClock(100.0)
        ids = DeterministicIds([order_id])
        service = OrderService(db_path, clock=clock, id_factory=ids)
        barrier.wait(timeout=15.0)
        try:
            order = service.create_order(
                idempotency_key,
                [{"sku": "shared", "quantity": 1, "unit_price_cents": 25}],
            )
        except OrderError as error:
            result_queue.put(("error", error.code, error.message, error.details))
        else:
            result_queue.put(("ok", order))
    except BaseException as error:
        result_queue.put(("worker_error", type(error).__name__, repr(error)))


def spool_claim_worker(
    root: str,
    worker_id: str,
    barrier: multiprocessing.synchronize.Barrier,
    result_queue: multiprocessing.queues.Queue,
) -> None:
    """Spawn-safe worker racing a claim against one shared state file."""

    try:
        from src.delivery_spool import Limits, Spool, SpoolError

        spool = Spool(
            root,
            clock=FakeClock(50.0),
            limits=Limits(),
            lock_timeout=10.0,
            failpoint=None,
        )
        barrier.wait(timeout=15.0)
        try:
            message = spool.claim(worker_id)
        except SpoolError as error:
            result_queue.put(("error", error.code, error.message, error.details))
        else:
            result_queue.put(("ok", message))
    except BaseException as error:
        result_queue.put(("worker_error", type(error).__name__, repr(error)))


def spool_hold_lock_worker(
    root: str,
    acquired_event: multiprocessing.synchronize.Event,
    release_event: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.queues.Queue,
) -> None:
    """Spawn-safe worker which holds the candidate's real spool lock at a failpoint."""

    class HolderReleased(RuntimeError):
        """Internal sentinel proving the held lock was released deliberately."""

    def hold_after_lock(name: str) -> None:
        if name == "after_lock":
            acquired_event.set()
            if not release_event.wait(timeout=15.0):
                raise TimeoutError("lock holder release safety timeout")
            raise HolderReleased("release without mutation")

    try:
        from src.delivery_spool import Limits, Spool

        spool = Spool(
            root,
            clock=FakeClock(0),
            limits=Limits(),
            lock_timeout=10.0,
            failpoint=hold_after_lock,
        )
        try:
            spool.enqueue("lock-holder", {}, available_at=0)
        except HolderReleased:
            result_queue.put(("released",))
        else:
            result_queue.put(("worker_error", "failpoint was not called"))
    except BaseException as error:
        result_queue.put(("worker_error", type(error).__name__, repr(error)))


def run_spawn_race(
    target: Callable[..., None],
    argument_rows: Sequence[tuple[object, ...]],
    *,
    timeout: float = 20.0,
) -> list[tuple[Any, ...]]:
    """Start spawn workers behind one real multiprocessing barrier and collect results."""

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(argument_rows))
    result_queue = context.Queue()
    processes = [
        context.Process(target=target, args=(*arguments, barrier, result_queue))
        for arguments in argument_rows
    ]
    for process in processes:
        process.start()
    results: list[tuple[Any, ...]] = []
    try:
        for _ in processes:
            try:
                results.append(result_queue.get(timeout=timeout))
            except queue.Empty as error:
                raise AssertionError("concurrency worker result timed out") from error
        for process in processes:
            process.join(timeout=timeout)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5.0)
                raise AssertionError("concurrency worker exceeded safety timeout")
            if process.exitcode != 0:
                raise AssertionError(f"concurrency worker exited with {process.exitcode}")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5.0)
        result_queue.close()
        result_queue.join_thread()
    return results


def direct_root_files(root: Path) -> set[str]:
    return {path.name for path in root.iterdir() if path.is_file()}


def assert_spool_root_boundary(case: Any, root: Path) -> None:
    entries = list(root.iterdir())
    case.assertFalse([path for path in entries if path.is_dir()])
    for path in entries:
        case.assertTrue(
            path.name in {"state.json", "state.lock"}
            or (path.name.startswith(".state.") and path.name.endswith(".tmp")),
            f"unexpected file under spool root: {path.name}",
        )
    case.assertFalse([path for path in entries if path.name.endswith(".tmp")])
