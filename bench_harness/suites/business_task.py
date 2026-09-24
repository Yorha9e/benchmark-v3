"""Business-axis long tasks: order fulfillment & inventory (task 1 of 2).

Replaces the algorithmic raft/saga axis as the default long suite. The model
implements a single stdlib-only module ``fulfillment.py`` exposing a
``FulfillmentService`` that owns one serial-transaction chain:

    add_stock -> create_order(idem) -> reserve(TTL) -> sweep_expired
              -> pay(gateway) -> fulfill -> ship
              -> deliver_next(spool, backoff, DLQ) -> redrive_dlq

plus crash recovery via a JSON journal. The scenario drives that API with
explicit clocks, scripted gateway failures and barrier-synchronised threads;
everything is deterministic (fixed seeds, explicit ``now``) so the suite can
be rescored offline at 0 LLM tokens, like the raft/saga axis before it.

The second business task (payment_ledger) lands in this module once task 1
is calibrated.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Task 1: order_fulfillment — contract (what the model reads as TASK.md)
# ---------------------------------------------------------------------------

FULFILLMENT_TITLE = "Order Fulfillment & Inventory Service"

FULFILLMENT_CONTRACT = """\
# Order Fulfillment & Inventory Service

You are implementing the fulfillment pipeline of an online store. The store
sells SKUs from a warehouse; customers place orders, stock is reserved with a
time-to-live, payment is taken through an external gateway, paid orders are
fulfilled and shipped through a delivery spool that retries with backoff and
dead-letters hopeless parcels. The service must survive a process restart
without losing or duplicating work.

Implement everything in ONE file: `fulfillment.py` (standard library only,
no network, no third-party packages). The grader imports your module and
drives the API below with an explicit clock. Every state transition must be
durable: the process may be killed at any point and restarted with the same
journal path.

## Required API

```python
class FulfillmentService:
    def __init__(self, journal_path: str | None = None,
                 delivery_max_attempts: int = 3,
                 delivery_backoff_seconds: float = 0.05) -> None: ...

    # -- inventory ---------------------------------------------------------
    def add_stock(self, sku: str, qty: int) -> None
        # Register/raise on-hand quantity for a sku. qty must be > 0.

    def stock_report(self) -> dict[str, dict[str, int]]
        # {sku: {"on_hand": n, "reserved": n, "available": n}} where
        # available == on_hand - reserved and must never be negative.

    # -- orders ------------------------------------------------------------
    def create_order(self, idem_key: str, lines: list[dict]) -> dict
        # lines: [{"sku": str, "qty": int}, ...]. Reject (ValueError) when:
        #   - lines is empty, or any qty <= 0, or any sku was never stocked.
        # Returns the order dict (see "Order dict" below). Replaying the SAME
        # idem_key must return the SAME order (same order_id) without creating
        # a second order and without side effects (no reservation, no stock).

    def order_status(self, order_id: str) -> dict | None
    def cancel_order(self, order_id: str) -> dict
        # Legal from PENDING, RESERVED and PAID; releases any reservation.

    # -- lifecycle (each returns the updated order dict) --------------------
    def reserve(self, order_id: str, ttl_seconds: float, now: float) -> dict
        # PENDING -> RESERVED; reserve the requested qty of every line.
        # Reject when available is insufficient (no partial reservation) or
        # the order is not PENDING.

    def sweep_expired(self, now: float) -> list[str]
        # Release every reservation whose expiry (reserve time + ttl_seconds)
        # is <= now: stock goes back on-hand, order -> CANCELLED. Returns the
        # cancelled order ids. Reservations not yet expired are untouched.

    def pay(self, order_id: str, idem_key: str, now: float,
            gateway: object) -> dict
        # RESERVED -> PAID. Call gateway.charge(idem_key, amount) exactly once
        # per distinct payment idem_key: charge returns True on success and
        # False on failure. Replaying pay with the same idem_key must NOT
        # charge again and must return the recorded outcome. On failure the
        # order is cancelled and its reservation released (compensation).

    def fulfill(self, order_id: str, now: float) -> dict
        # PAID -> FULFILLED.

    # -- delivery spool ------------------------------------------------------
    def ship(self, order_id: str, now: float) -> dict
        # FULFILLED -> SHIPPED; the order enters the delivery spool.

    def deliver_next(self, now: float) -> dict | None
        # Attempt the next spool entry whose backoff allows it (an entry that
        # failed at time t may only be attempted again at >= t + backoff).
        # Calls the delivery gateway set via set_delivery_gateway():
        # deliver(order_id) -> bool. Returns the entry dict (see "Delivery
        # entry") or None when nothing is due. After delivery_max_attempts
        # failed attempts the entry moves to the dead-letter queue; a
        # successful delivery removes it from the spool.

    def set_delivery_gateway(self, gateway: object) -> None
    def dlq(self) -> list[str]            # order ids currently dead-lettered
    def redrive_dlq(self, now: float) -> list[str]
        # Move every dead-lettered order back into the spool (attempts reset).
```

## Order dict

```python
{
  "order_id": str,          # stable, unique, assigned by you
  "idem_key": str,
  "state": str,             # PENDING RESERVED PAID FULFILLED SHIPPED
                            # DELIVERED CANCELLED
  "lines": [{"sku": str, "qty": int}],
  "total_qty": int,
  "created_at": float,
  "updated_at": float,
  "payment_attempts": int,  # gateway charge calls caused by this order
}
```

Legal transitions: PENDING->RESERVED->PAID->FULFILLED->SHIPPED->DELIVERED;
PENDING/RESERVED/PAID->CANCELLED (cancel_order, pay-failure compensation,
TTL expiry). DELIVERED and CANCELLED are terminal. Any other transition must
be rejected (ValueError) and must leave the order untouched.

## Delivery entry dict (spool / dlq bookkeeping is yours)

```python
{"order_id": str, "attempts": int, "last_attempt_at": float | None}
```

## Durability

`__init__(journal_path)` must load prior state from that file when it exists
(orders, reservations, spool, dlq, stock, and the id counter) and every
mutating call must keep the journal current on disk, so a fresh
FulfillmentService with the same journal_path resumes exactly where the
killed one left off - no lost reservations, no double deliveries, no
duplicate orders, no reused ids. When `journal_path` is None the service
runs purely in memory (no file writes).

## Rules

- Standard library only; the grader may call the API from several threads at
  once (concurrent reservations and payments on the same service must be
  safe) and will drive the clock explicitly - never call time.time()
  yourself for expiry/backoff decisions; use the `now` argument.
- bash/`python -c` prototypes are not scored; only `fulfillment.py` matters.
- When the file is ready, call the `finish` tool with a summary of what you
  implemented and how you checked it.
"""

# ---------------------------------------------------------------------------
# Reference implementation (self-test oracle; NOT shown to the model)
# ---------------------------------------------------------------------------

REFERENCE_FULFILLMENT = '''"""Order fulfillment & inventory service (reference, stdlib only)."""
from __future__ import annotations

import json
import os
import threading

_PENDING = "PENDING"
_RESERVED = "RESERVED"
_PAID = "PAID"
_FULFILLED = "FULFILLED"
_SHIPPED = "SHIPPED"
_DELIVERED = "DELIVERED"
_CANCELLED = "CANCELLED"


class FulfillmentService:
    def __init__(self, journal_path=None, delivery_max_attempts=3,
                 delivery_backoff_seconds=0.05):
        self.journal_path = journal_path
        self.delivery_max_attempts = int(delivery_max_attempts)
        self.delivery_backoff_seconds = float(delivery_backoff_seconds)
        self._lock = threading.RLock()
        self._delivery_gateway = None
        self._on_hand = {}
        self._orders = {}
        self._by_idem = {}
        self._payments = {}
        self._spool = {}
        self._dlq = {}
        self._counter = 0
        if journal_path:
            try:
                with open(journal_path, "r", encoding="utf-8") as fh:
                    self._load(json.load(fh))
            except FileNotFoundError:
                pass

    # -- persistence -------------------------------------------------------
    def _snapshot(self):
        return {
            "on_hand": self._on_hand, "orders": self._orders,
            "by_idem": self._by_idem, "payments": self._payments,
            "spool": self._spool, "dlq": self._dlq, "counter": self._counter,
        }

    def _load(self, state):
        self._on_hand = dict(state.get("on_hand") or {})
        self._orders = dict(state.get("orders") or {})
        self._by_idem = dict(state.get("by_idem") or {})
        self._payments = dict(state.get("payments") or {})
        self._spool = dict(state.get("spool") or {})
        self._dlq = dict(state.get("dlq") or {})
        self._counter = int(state.get("counter") or 0)

    def _flush(self):
        if not self.journal_path:
            return
        tmp = self.journal_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._snapshot(), fh)
        os.replace(tmp, self.journal_path)

    def _next_id(self):
        self._counter += 1
        return "ord-%06d" % self._counter

    # -- inventory -----------------------------------------------------------
    def add_stock(self, sku, qty):
        qty = int(qty)
        if qty <= 0:
            raise ValueError("qty must be positive")
        with self._lock:
            self._on_hand[sku] = self._on_hand.get(sku, 0) + qty
            self._flush()

    def stock_report(self):
        with self._lock:
            out = {}
            for sku, on_hand in self._on_hand.items():
                reserved = 0
                for o in self._orders.values():
                    # committed = RESERVED/PAID/FULFILLED/SHIPPED: stock stays
                    # locked until the order reaches a terminal state.
                    if o["state"] not in (_RESERVED, _PAID, _FULFILLED, _SHIPPED):
                        continue
                    for line in o["lines"]:
                        if line["sku"] == sku:
                            reserved += line["qty"]
                out[sku] = {"on_hand": on_hand, "reserved": reserved,
                            "available": on_hand - reserved}
            return out

    # -- orders ---------------------------------------------------------------
    def create_order(self, idem_key, lines):
        if not lines:
            raise ValueError("empty order")
        norm = []
        for line in lines:
            qty = int(line["qty"])
            if qty <= 0:
                raise ValueError("qty must be positive")
            norm.append({"sku": str(line["sku"]), "qty": qty})
        with self._lock:
            known = self._by_idem.get(idem_key)
            if known is not None:
                return self._view(known)
            for line in norm:
                if line["sku"] not in self._on_hand:
                    raise ValueError("unknown sku: %s" % line["sku"])
            oid = self._next_id()
            self._orders[oid] = {
                "order_id": oid, "idem_key": idem_key, "state": _PENDING,
                "lines": norm, "total_qty": sum(l["qty"] for l in norm),
                "created_at": None, "updated_at": None,
                "payment_attempts": 0,
            }
            self._by_idem[idem_key] = oid
            self._flush()
            return self._view(oid)

    def order_status(self, order_id):
        with self._lock:
            if order_id not in self._orders:
                return None
            return self._view(order_id)

    def _view(self, order_id):
        o = self._orders[order_id]
        return {
            "order_id": o["order_id"], "idem_key": o["idem_key"],
            "state": o["state"], "lines": [dict(l) for l in o["lines"]],
            "total_qty": o["total_qty"], "created_at": o["created_at"],
            "updated_at": o["updated_at"],
            "payment_attempts": o["payment_attempts"],
        }

    def cancel_order(self, order_id):
        with self._lock:
            o = self._orders.get(order_id)
            if o is None:
                raise ValueError("unknown order")
            if o["state"] in (_DELIVERED, _CANCELLED):
                raise ValueError("terminal order")
            o["state"] = _CANCELLED
            self._flush()
            return self._view(order_id)

    # -- lifecycle --------------------------------------------------------------
    def reserve(self, order_id, ttl_seconds, now):
        with self._lock:
            o = self._orders.get(order_id)
            if o is None:
                raise ValueError("unknown order")
            if o["state"] != _PENDING:
                raise ValueError("order is not PENDING")
            need = {}
            for line in o["lines"]:
                need[line["sku"]] = need.get(line["sku"], 0) + line["qty"]
            report = self.stock_report()
            for sku, qty in need.items():
                if report.get(sku, {}).get("available", 0) < qty:
                    raise ValueError("insufficient stock for %s" % sku)
            o["state"] = _RESERVED
            o["created_at"] = now
            o["updated_at"] = now
            o["ttl"] = float(ttl_seconds)
            o["reserved_at"] = now
            self._flush()
            return self._view(order_id)

    def sweep_expired(self, now):
        with self._lock:
            cancelled = []
            for oid, o in list(self._orders.items()):
                if o["state"] != _RESERVED:
                    continue
                if o.get("reserved_at") is not None and now >= o["reserved_at"] + o.get("ttl", 0.0):
                    o["state"] = _CANCELLED
                    o["updated_at"] = now
                    cancelled.append(oid)
            if cancelled:
                self._flush()
            return cancelled

    def pay(self, order_id, idem_key, now, gateway):
        with self._lock:
            o = self._orders.get(order_id)
            if o is None:
                raise ValueError("unknown order")
            prior = self._payments.get(idem_key)
            if prior is not None:
                return self._view(order_id)
            if o["state"] != _RESERVED:
                raise ValueError("order is not RESERVED")
            ok = bool(gateway.charge(idem_key, o["total_qty"]))
            o["payment_attempts"] += 1
            self._payments[idem_key] = {"order_id": order_id, "ok": ok}
            o["state"] = _PAID if ok else _CANCELLED
            o["updated_at"] = now
            self._flush()
            return self._view(order_id)

    def fulfill(self, order_id, now):
        with self._lock:
            o = self._orders.get(order_id)
            if o is None or o["state"] != _PAID:
                raise ValueError("order is not PAID")
            o["state"] = _FULFILLED
            o["updated_at"] = now
            self._flush()
            return self._view(order_id)

    # -- delivery spool ------------------------------------------------------------
    def set_delivery_gateway(self, gateway):
        self._delivery_gateway = gateway

    def ship(self, order_id, now):
        with self._lock:
            o = self._orders.get(order_id)
            if o is None or o["state"] != _FULFILLED:
                raise ValueError("order is not FULFILLED")
            o["state"] = _SHIPPED
            o["updated_at"] = now
            self._spool[order_id] = {"order_id": order_id, "attempts": 0,
                                     "last_attempt_at": None}
            self._flush()
            return self._view(order_id)

    def deliver_next(self, now):
        with self._lock:
            due = None
            for entry in self._spool.values():
                last = entry["last_attempt_at"]
                if last is None or now >= last + self.delivery_backoff_seconds:
                    due = entry
                    break
            if due is None:
                return None
            due["attempts"] += 1
            due["last_attempt_at"] = now
            ok = bool(self._delivery_gateway.deliver(due["order_id"]))
            entry = dict(due)
            if ok:
                o = self._orders[due["order_id"]]
                o["state"] = _DELIVERED
                o["updated_at"] = now
                del self._spool[due["order_id"]]
            elif due["attempts"] >= self.delivery_max_attempts:
                del self._spool[due["order_id"]]
                self._dlq[due["order_id"]] = entry
            self._flush()
            return entry

    def dlq(self):
        with self._lock:
            return sorted(self._dlq)

    def redrive_dlq(self, now):
        with self._lock:
            moved = []
            for oid in sorted(self._dlq):
                entry = self._dlq.pop(oid)
                entry["attempts"] = 0
                entry["last_attempt_at"] = None
                self._spool[oid] = entry
                moved.append(oid)
            if moved:
                self._flush()
            return moved
'''


# ---------------------------------------------------------------------------
# Scenario harness: drives the model's FulfillmentService through probes
# ---------------------------------------------------------------------------

class _PaymentGateway:
    """Scenario-controlled payment gateway: fails the listed idem keys."""

    def __init__(self, fail_keys=()):
        self.calls: list[tuple[str, int]] = []
        self._fail = set(fail_keys)

    def charge(self, idem_key: str, amount: int) -> bool:
        self.calls.append((idem_key, amount))
        return idem_key not in self._fail


class _DeliveryGateway:
    """Scenario-controlled delivery gateway: fails the listed order ids."""

    def __init__(self, fail_ids=()):
        self.calls: list[str] = []
        self._fail = set(fail_ids)

    def deliver(self, order_id: str) -> bool:
        self.calls.append(order_id)
        return order_id not in self._fail


def _load_service_class(workspace_dir: str | Path):
    """Import the model's fulfillment.py and return FulfillmentService."""
    path = Path(workspace_dir) / "fulfillment.py"
    if not path.is_file():
        raise FileNotFoundError("fulfillment.py missing")
    spec = importlib.util.spec_from_file_location("model_fulfillment", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.FulfillmentService


_RESTART_RUNNER = '''"""Restart-probe child: drives the model service, then dies hard."""
import importlib.util
import json
import os
import sys

module_path, journal_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

spec = importlib.util.spec_from_file_location("model_fulfillment", module_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class GW:
    def __init__(self):
        self.calls = []

    def charge(self, key, amount):
        self.calls.append(key)
        return True


class DG:
    def __init__(self, fail):
        self.fail = set(fail)
        self.calls = []

    def deliver(self, oid):
        self.calls.append(oid)
        return oid not in self.fail


svc = mod.FulfillmentService(journal_path=journal_path,
                             delivery_max_attempts=3,
                             delivery_backoff_seconds=0.05)
gw, dg = GW(), DG(fail=[])
svc.add_stock("skuA", 10)
o1 = svc.create_order("k1", [{"sku": "skuA", "qty": 2}])
o2 = svc.create_order("k2", [{"sku": "skuA", "qty": 3}])
svc.reserve(o1["order_id"], 100.0, now=1.0)
svc.reserve(o2["order_id"], 100.0, now=1.0)
svc.pay(o1["order_id"], "p1", now=2.0, gateway=gw)
svc.fulfill(o1["order_id"], now=3.0)
svc.set_delivery_gateway(dg)
svc.ship(o1["order_id"], now=4.0)
# one failed delivery attempt so the spool entry carries attempts=1
dg.fail.add(o1["order_id"])
svc.deliver_next(now=5.0)
# hand the created ids back so the parent probe never hardcodes a format
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({"o1": o1["order_id"], "o2": o2["order_id"]}, fh)
# kill hard: no flush hooks, no atexit — whatever is on disk is what counts
os._exit(9)
'''


def _run_restart_probe(workspace_dir: str | Path) -> dict[str, Any]:
    """Phase 1 in a hard-killed child; phase 2 re-opens the same journal."""
    with tempfile.TemporaryDirectory(prefix="biz-restart-") as tmp:
        module_path = str(Path(workspace_dir) / "fulfillment.py")
        journal = str(Path(tmp) / "journal.json")
        runner = Path(tmp) / "restart_runner.py"
        runner.write_text(_RESTART_RUNNER, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(runner), module_path, journal, str(Path(tmp) / "out.json")],
            capture_output=True, text=True, timeout=60)
        survived = proc.returncode != 0
        recovered: dict[str, Any] = {"killed": survived}
        if not Path(journal).is_file():
            recovered["journal_missing"] = True
            return recovered
        try:
            FulfillmentService = _load_service_class(workspace_dir)
            svc = FulfillmentService(journal_path=journal,
                                     delivery_max_attempts=3,
                                     delivery_backoff_seconds=0.05)
        except Exception as exc:  # noqa: BLE001 - report as probe failure
            recovered["reload_error"] = repr(exc)
            return recovered

        ids = {}
        out_file = Path(tmp) / "out.json"
        if out_file.is_file():
            try:
                ids = json.loads(out_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                ids = {}
        o1 = svc.order_status(str(ids.get("o1", "")))
        o2 = svc.order_status(str(ids.get("o2", "")))
        recovered["ids_recovered"] = bool(ids)
        recovered["o1_state"] = (o1 or {}).get("state")
        recovered["o2_state"] = (o2 or {}).get("state")
        rep = svc.stock_report()
        recovered["report"] = rep
        # replaying the child's idem key must not create a duplicate order
        try:
            replay = svc.create_order("k1", [{"sku": "skuA", "qty": 2}])
            recovered["replay_same_id"] = (
                bool(ids.get("o1")) and replay["order_id"] == ids.get("o1"))
            recovered["order_count"] = 1 if replay["order_id"] == ids.get("o1") else 2
        except Exception as exc:  # noqa: BLE001
            recovered["replay_error"] = repr(exc)
        # spool backoff must survive: an immediate re-attempt is refused
        dg = _DeliveryGateway(fail_ids=())
        svc.set_delivery_gateway(dg)
        immediate = svc.deliver_next(now=5.0)   # last attempt was at t=5.0
        recovered["spool_respects_backoff"] = immediate is None
        later = svc.deliver_next(now=5.06)
        recovered["spool_attempt_after_backoff"] = later is not None
        recovered["deliver_calls_after_recovery"] = len(dg.calls)
        _rep = recovered.get("report") or {}
        _a = _rep.get("skuA") or {}
        recovered["recovered_report_ok"] = (
            _a.get("reserved") == 5 and _a.get("available") == 5
            and _a.get("on_hand") == 10)
        return recovered


SCENARIO_TIMEOUT_SECONDS = 240


def run_fulfillment_scenario(workspace_dir: str | Path) -> dict[str, Any]:
    """Drive the model's service through every probe; return raw evidence.

    The model's module is UNTRUSTED CODE: it runs in a child process with a
    hard timeout (a deadlocked or crashing fulfillment.py must never take
    the harness down). Evidence comes back as JSON; a child failure fails
    closed with ``fatal`` set so every milestone scores 0.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker",
             str(workspace_dir)],
            capture_output=True, text=True, timeout=SCENARIO_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"fatal": "scenario worker timeout after %ds"
                        % SCENARIO_TIMEOUT_SECONDS}
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:]
        return {"fatal": "scenario worker exit %d: %s" % (proc.returncode, tail)}
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {"fatal": "scenario worker output unreadable: %r" % exc}
    if not isinstance(data, dict):
        return {"fatal": "scenario worker returned non-dict"}
    return data


def _scenario_worker(workspace_dir: str | Path) -> dict[str, Any]:
    """In-process scenario body (child mode; see run_fulfillment_scenario)."""
    out: dict[str, Any] = {}
    FulfillmentService = _load_service_class(workspace_dir)
    svc = FulfillmentService()

    # -- P1: validation + idempotency --------------------------------------
    p1: dict[str, Any] = {}
    try:
        svc.add_stock("skuA", 10)
        svc.add_stock("skuB", 4)
        p1["stock_ok"] = True
    except Exception as exc:  # noqa: BLE001
        p1["stock_ok"] = False
        p1["stock_error"] = repr(exc)
    rejects = {}
    for name, fn in (
        ("empty_lines", lambda: svc.create_order("bad", [])),
        ("qty_zero", lambda: svc.create_order("bad", [{"sku": "skuA", "qty": 0}])),
        ("qty_negative", lambda: svc.create_order("bad", [{"sku": "skuA", "qty": -2}])),
        ("unknown_sku", lambda: svc.create_order("bad", [{"sku": "nope", "qty": 1}])),
    ):
        try:
            fn()
            rejects[name] = False
        except ValueError:
            rejects[name] = True
        except Exception as exc:  # noqa: BLE001
            rejects[name] = f"wrong-exc:{type(exc).__name__}"
    p1["rejects"] = rejects
    try:
        first = svc.create_order("idem-1", [{"sku": "skuA", "qty": 2}])
        replay = svc.create_order("idem-1", [{"sku": "skuA", "qty": 2}])
        p1["idem_same_order"] = first["order_id"] == replay["order_id"]
        p1["idem_state_pending"] = replay["state"] == "PENDING"
        p1["idem_no_stock_effect"] = (
            svc.stock_report()["skuA"]["reserved"] == 0)
        p1["idem_available_full"] = (
            svc.stock_report()["skuA"]["available"] == 10)
        p1["order_fields"] = all(
            k in first for k in ("order_id", "idem_key", "state", "lines",
                                 "total_qty", "created_at", "updated_at",
                                 "payment_attempts"))
    except Exception as exc:  # noqa: BLE001
        p1["idem_error"] = repr(exc)
    out["p1"] = p1

    # -- P2: reserve correctness + insufficient rejection --------------------
    p2: dict[str, Any] = {}
    try:
        o = svc.create_order("res-1", [{"sku": "skuA", "qty": 3}])
        r = svc.reserve(o["order_id"], 100.0, now=1.0)
        p2["reserved_state"] = r["state"] == "RESERVED"
        rep = svc.stock_report()
        p2["report_consistent"] = all(
            v["available"] == v["on_hand"] - v["reserved"] and v["available"] >= 0
            for v in rep.values())
        p2["reserved_qty"] = rep["skuA"]["reserved"]
        big = svc.create_order("res-2", [{"sku": "skuA", "qty": 99}])
        try:
            svc.reserve(big["order_id"], 100.0, now=1.0)
            p2["insufficient_rejected"] = False
        except ValueError:
            p2["insufficient_rejected"] = True
        p2["no_partial_reserve"] = svc.stock_report()["skuA"]["reserved"] == 3
        p2["order_still_pending"] = svc.order_status(big["order_id"])["state"] == "PENDING"
        double = svc.create_order("res-3", [{"sku": "skuA", "qty": 1}])
        svc.reserve(double["order_id"], 100.0, now=1.0)
        try:
            svc.reserve(double["order_id"], 100.0, now=2.0)
            p2["double_reserve_rejected"] = False
        except ValueError:
            p2["double_reserve_rejected"] = True
    except Exception as exc:  # noqa: BLE001
        p2["error"] = repr(exc)
    out["p2"] = p2

    # -- P3: TTL expiry (fresh service: sweep_expired is global) ---------------
    p3: dict[str, Any] = {}
    try:
        svc_t = FulfillmentService()
        svc_t.add_stock("skuA", 10)
        t1 = svc_t.create_order("ttl-1", [{"sku": "skuA", "qty": 1}])
        t2 = svc_t.create_order("ttl-2", [{"sku": "skuA", "qty": 1}])
        svc_t.reserve(t1["order_id"], 10.0, now=100.0)
        svc_t.reserve(t2["order_id"], 50.0, now=100.0)
        early = svc_t.sweep_expired(now=105.0)
        p3["early_sweep_empty"] = early == []
        p3["early_untouched"] = svc_t.order_status(t1["order_id"])["state"] == "RESERVED"
        late = svc_t.sweep_expired(now=110.0)
        p3["expired_cancelled"] = t1["order_id"] in late and t2["order_id"] not in late
        p3["expired_state"] = svc_t.order_status(t1["order_id"])["state"] == "CANCELLED"
        rep = svc_t.stock_report()
        p3["stock_restored"] = rep["skuA"]["reserved"] == 1  # only t2 remains
        p3["stock_restored_exact"] = rep["skuA"]["available"] == rep["skuA"]["on_hand"] - 1
        again = svc_t.sweep_expired(now=200.0)
        p3["sweep_idempotent"] = t1["order_id"] not in again
    except Exception as exc:  # noqa: BLE001
        p3["error"] = repr(exc)
    out["p3"] = p3

    # -- P4: payment idempotency + failure compensation ------------------------
    p4: dict[str, Any] = {}
    try:
        pay_order = svc.create_order("pay-1", [{"sku": "skuB", "qty": 2}])
        svc.reserve(pay_order["order_id"], 100.0, now=1.0)
        gw = _PaymentGateway()
        r1 = svc.pay(pay_order["order_id"], "pk-1", now=2.0, gateway=gw)
        p4["paid"] = r1["state"] == "PAID"
        p4["charge_once"] = gw.calls == [("pk-1", 2)]
        r2 = svc.pay(pay_order["order_id"], "pk-1", now=3.0, gateway=gw)
        p4["replay_no_recharge"] = len(gw.calls) == 1
        p4["replay_state"] = r2["state"] == "PAID"
        # failing gateway -> compensate
        bad = svc.create_order("pay-2", [{"sku": "skuB", "qty": 1}])
        svc.reserve(bad["order_id"], 100.0, now=1.0)
        before = svc.stock_report()["skuB"]["reserved"]
        gwf = _PaymentGateway(fail_keys=("pk-2",))
        rf = svc.pay(bad["order_id"], "pk-2", now=2.0, gateway=gwf)
        p4["fail_cancelled"] = rf["state"] == "CANCELLED"
        p4["fail_compensated"] = svc.stock_report()["skuB"]["reserved"] == before - 1
        p4["fail_charged_once"] = gwf.calls == [("pk-2", 1)]
        try:
            svc.pay(bad["order_id"], "pk-3", now=3.0, gateway=gwf)
            p4["pay_after_cancel_rejected"] = False
        except ValueError:
            p4["pay_after_cancel_rejected"] = True
    except Exception as exc:  # noqa: BLE001
        p4["error"] = repr(exc)
    out["p4"] = p4

    # -- P5: state machine legality ---------------------------------------------
    p5: dict[str, Any] = {}
    try:
        a = svc.create_order("sm-1", [{"sku": "skuA", "qty": 1}])
        svc.reserve(a["order_id"], 100.0, now=1.0)
        gw = _PaymentGateway()
        svc.pay(a["order_id"], "sm-pay", now=2.0, gateway=gw)
        checks = {}
        try:
            svc.reserve(a["order_id"], 1.0, now=3.0)
            checks["reserve_on_paid"] = False
        except ValueError:
            checks["reserve_on_paid"] = True
        checks["state_after_reserve_attempt"] = (
            svc.order_status(a["order_id"])["state"] == "PAID")
        b = svc.create_order("sm-2", [{"sku": "skuA", "qty": 1}])
        try:
            svc.fulfill(b["order_id"], now=1.0)
            checks["fulfill_on_pending"] = False
        except ValueError:
            checks["fulfill_on_pending"] = True
        try:
            svc.ship(a["order_id"], now=3.0)
            checks["ship_on_paid"] = False
        except ValueError:
            checks["ship_on_paid"] = True
        checks["state_after_ship_attempt"] = (
            svc.order_status(a["order_id"])["state"] == "PAID")
        p5.update(checks)
        # legal path completes
        svc.fulfill(a["order_id"], now=4.0)
        svc.set_delivery_gateway(_DeliveryGateway())
        svc.ship(a["order_id"], now=5.0)
        e = svc.deliver_next(now=6.0)
        p5["delivered"] = bool(e) and svc.order_status(a["order_id"])["state"] == "DELIVERED"
        try:
            svc.cancel_order(a["order_id"])
            p5["cancel_delivered_rejected"] = False
        except ValueError:
            p5["cancel_delivered_rejected"] = True
    except Exception as exc:  # noqa: BLE001
        p5["error"] = repr(exc)
    out["p5"] = p5

    # -- P6: delivery spool backoff / attempts / DLQ / redrive --------------------
    p6: dict[str, Any] = {}
    try:
        s1 = svc.create_order("sp-1", [{"sku": "skuA", "qty": 1}])
        svc.reserve(s1["order_id"], 100.0, now=1.0)
        svc.pay(s1["order_id"], "sp-pay", now=2.0, gateway=_PaymentGateway())
        svc.fulfill(s1["order_id"], now=3.0)
        svc.set_delivery_gateway(_DeliveryGateway(fail_ids=(s1["order_id"],)))
        svc.ship(s1["order_id"], now=4.0)
        e1 = svc.deliver_next(now=5.0)
        p6["first_attempt"] = bool(e1) and e1["attempts"] == 1
        early = svc.deliver_next(now=5.02)
        p6["backoff_enforced"] = early is None
        e2 = svc.deliver_next(now=5.06)
        p6["second_attempt_after_backoff"] = bool(e2) and e2["attempts"] == 2
        e3 = svc.deliver_next(now=5.12)
        p6["third_attempt"] = bool(e3) and e3["attempts"] == 3
        p6["dlq_after_max"] = s1["order_id"] in svc.dlq()
        p6["spool_empty_after_dlq"] = svc.deliver_next(now=5.2) is None
        moved = svc.redrive_dlq(now=6.0)
        p6["redrive_moves_back"] = s1["order_id"] in moved
        # attempts reset: the first post-redrive attempt counts 1 (still failing)
        e_rd = svc.deliver_next(now=6.0)
        p6["redrive_resets_attempts"] = bool(e_rd) and e_rd["attempts"] == 1
        # delivery now succeeds; attempt allowed after the backoff window
        svc.set_delivery_gateway(_DeliveryGateway())
        ok = svc.deliver_next(now=6.06)
        p6["success_after_redrive"] = bool(ok)
        p6["delivered_state"] = svc.order_status(s1["order_id"])["state"] == "DELIVERED"
        p6["spool_drained"] = svc.deliver_next(now=8.0) is None
    except Exception as exc:  # noqa: BLE001
        p6["error"] = repr(exc)
    out["p6"] = p6

    # -- P7: concurrency (oversell + churn conservation) ---------------------------
    p7: dict[str, Any] = {}
    try:
        svc2 = FulfillmentService()
        svc2.add_stock("hot", 1)
        barrier = threading.Barrier(8)
        results: list[str] = []
        res_lock = threading.Lock()

        def _grab(i: int) -> None:
            try:
                barrier.wait(timeout=5)
                o = svc2.create_order("cc-%d" % i, [{"sku": "hot", "qty": 1}])
                svc2.reserve(o["order_id"], 100.0, now=1.0)
                with res_lock:
                    results.append("ok")
            except ValueError:
                with res_lock:
                    results.append("rejected")
            except Exception as exc:  # noqa: BLE001
                with res_lock:
                    results.append("err:%s" % type(exc).__name__)

        threads = [threading.Thread(target=_grab, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        p7["oversell_exactly_one"] = results.count("ok") == 1
        p7["oversell_rejects"] = results.count("rejected")
        p7["oversell_no_crash"] = not any(r.startswith("err:") for r in results)
        rep = svc2.stock_report()["hot"]
        p7["no_negative_stock"] = rep["available"] >= 0
        p7["stock_exact"] = rep["reserved"] == 1 and rep["on_hand"] == 1
        # churn: 8 threads reserve+sweep their own orders; stock must return
        svc3 = FulfillmentService()
        svc3.add_stock("churn", 40)
        barrier2 = threading.Barrier(8)

        def _churn(i: int) -> None:
            try:
                o = svc3.create_order("ch-%d" % i, [{"sku": "churn", "qty": 5}])
                barrier2.wait(timeout=5)
                svc3.reserve(o["order_id"], 1.0, now=1.0)
            except Exception:  # noqa: BLE001 - rejections are fine, we count stock
                pass

        threads = [threading.Thread(target=_churn, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        svc3.sweep_expired(now=100.0)
        rep3 = svc3.stock_report()["churn"]
        p7["churn_stock_restored"] = rep3["reserved"] == 0 and rep3["available"] == 40
        p7["churn_on_hand"] = rep3["on_hand"] == 40
    except Exception as exc:  # noqa: BLE001
        p7["error"] = repr(exc)
    out["p7"] = p7

    # -- P8: crash recovery -------------------------------------------------------
    try:
        out["p8"] = _run_restart_probe(workspace_dir)
    except Exception as exc:  # noqa: BLE001
        out["p8"] = {"error": repr(exc)}
    return out



# ---------------------------------------------------------------------------
# Milestone builder: probe evidence -> 11 milestones (fractional scores)
# ---------------------------------------------------------------------------

_MILESTONE_NAMES = (
    "下单校验与订单结构", "下单幂等", "库存预留与不足拒绝", "并发超卖防护与守恒",
    "TTL 过期释放", "支付幂等", "支付失败补偿", "状态机合法性",
    "发货重试与退避", "DLQ 重驱动", "崩溃恢复",
)


def _fm(idx: int, name: str, checks: list[tuple[bool, str]]):
    """One milestone from (ok, label) assertions; score = passed/total."""
    from benchmark_v3.bench_harness.core.types import MilestoneResult

    passed = sum(1 for ok, _ in checks if ok)
    total = len(checks) or 1
    detail = "; ".join("%s %s" % ("PASS" if ok else "FAIL", label)
                       for ok, label in checks)
    return MilestoneResult(
        milestone_id="order_fulfillment_m%d" % idx,
        name=name,
        passed=passed == total,
        score=round(passed / total, 3),
        failure_reason=None if passed == total else detail,
        diagnostics=detail,
    )


def build_fulfillment_milestones(data: dict[str, Any]) -> list[Any]:
    """Map scenario evidence to the 11 order_fulfillment milestones."""
    if data.get("fatal"):
        # fail closed: untrusted code crashed / hung / missing
        return [_fm(i + 1, name, [(False, "fatal: %s" % str(data["fatal"])[:120])])
                for i, name in enumerate(_MILESTONE_NAMES)]
    # A probe phase that recorded an ERROR (the model's code raised where it
    # should not) marks its milestone as ERROR rather than plain FAIL — an
    # exception is not the same evidence as a wrong value.
    _errored = {k for k, v in data.items()
                if isinstance(v, dict) and v.get("error")}
    p1 = data.get("p1") or {}
    p2 = data.get("p2") or {}
    p3 = data.get("p3") or {}
    p4 = data.get("p4") or {}
    p5 = data.get("p5") or {}
    p6 = data.get("p6") or {}
    p7 = data.get("p7") or {}
    p8 = data.get("p8") or {}
    rej = p1.get("rejects") or {}

    def ok(section: dict, key: str) -> bool:
        return section.get(key) is True

    ms: list[Any] = []
    ms.append(_fm(1, _MILESTONE_NAMES[0], [
        (rej.get("empty_lines") is True, "空行拒绝"),
        (rej.get("qty_zero") is True, "qty=0 拒绝"),
        (rej.get("qty_negative") is True, "负 qty 拒绝"),
        (rej.get("unknown_sku") is True, "未备货 sku 拒绝"),
        (ok(p1, "order_fields"), "订单字段完整"),
    ]))
    ms.append(_fm(2, _MILESTONE_NAMES[1], [
        (ok(p1, "idem_same_order"), "同键重放同订单"),
        (ok(p1, "idem_state_pending"), "重放不推进状态"),
        (ok(p1, "idem_no_stock_effect"), "重放不占库存"),
        (ok(p1, "idem_available_full"), "重放后 available 不变"),
    ]))
    ms.append(_fm(3, _MILESTONE_NAMES[2], [
        (ok(p2, "reserved_state"), "预留后 RESERVED"),
        (ok(p2, "report_consistent"), "报表自洽"),
        (p2.get("reserved_qty") == 3, "预留数量正确"),
        (ok(p2, "insufficient_rejected"), "库存不足拒绝"),
        (ok(p2, "no_partial_reserve"), "无部分预留"),
        (ok(p2, "double_reserve_rejected"), "重复预留拒绝"),
    ]))
    ms.append(_fm(4, _MILESTONE_NAMES[3], [
        (ok(p7, "oversell_exactly_one"), "8 线程抢 1 件恰好 1 成功"),
        (ok(p7, "oversell_no_crash"), "并发无异常逃逸"),
        (ok(p7, "no_negative_stock"), "无负库存"),
        (ok(p7, "stock_exact"), "终态库存精确"),
        (ok(p7, "churn_stock_restored"), "churn 后库存完整回补"),
    ]))
    ms.append(_fm(5, _MILESTONE_NAMES[4], [
        (ok(p3, "early_sweep_empty"), "未过期不动"),
        (ok(p3, "early_untouched"), "未过期订单仍 RESERVED"),
        (ok(p3, "expired_cancelled"), "过期取消且不影响他单"),
        (ok(p3, "expired_state"), "过期后 CANCELLED"),
        (ok(p3, "stock_restored_exact"), "库存精确回补"),
        (ok(p3, "sweep_idempotent"), "sweep 幂等"),
    ]))
    ms.append(_fm(6, _MILESTONE_NAMES[5], [
        (ok(p4, "paid"), "支付成功 PAID"),
        (ok(p4, "charge_once"), "扣款恰好一次且金额正确"),
        (ok(p4, "replay_no_recharge"), "重放不二次扣款"),
        (ok(p4, "replay_state"), "重放状态保持"),
    ]))
    ms.append(_fm(7, _MILESTONE_NAMES[6], [
        (ok(p4, "fail_cancelled"), "失败取消订单"),
        (ok(p4, "fail_compensated"), "预留释放"),
        (ok(p4, "fail_charged_once"), "失败也只扣一次"),
        (ok(p4, "pay_after_cancel_rejected"), "取消后支付拒绝"),
    ]))
    ms.append(_fm(8, _MILESTONE_NAMES[7], [
        (ok(p5, "reserve_on_paid"), "PAID 后预留拒绝"),
        (ok(p5, "fulfill_on_pending"), "PENDING 履约拒绝"),
        (ok(p5, "ship_on_paid"), "PAID 发货拒绝"),
        (ok(p5, "state_after_ship_attempt"), "拒绝后状态无损"),
        (ok(p5, "cancel_delivered_rejected"), "DELIVERED 取消拒绝"),
        (ok(p5, "delivered"), "合法全链路放行"),
    ]))
    ms.append(_fm(9, _MILESTONE_NAMES[8], [
        (ok(p6, "first_attempt"), "首次尝试记录"),
        (ok(p6, "backoff_enforced"), "退避窗口内拒绝"),
        (ok(p6, "second_attempt_after_backoff"), "退避后放行"),
        (ok(p6, "third_attempt"), "第三次尝试"),
        (ok(p6, "dlq_after_max"), "超限进 DLQ"),
    ]))
    ms.append(_fm(10, _MILESTONE_NAMES[9], [
        (ok(p6, "redrive_moves_back"), "重驱动回到 spool"),
        (ok(p6, "redrive_resets_attempts"), "attempts 重置"),
        (ok(p6, "success_after_redrive"), "重驱动后可投递"),
        (ok(p6, "spool_drained"), "成功后 spool 清空"),
    ]))
    ms.append(_fm(11, _MILESTONE_NAMES[10], [
        (p8.get("ids_recovered") is True, "子进程回传 id"),
        (p8.get("o1_state") == "SHIPPED" and p8.get("o2_state") == "RESERVED",
         "订单状态恢复"),
        (ok(p8, "replay_same_id"), "恢复后幂等键仍有效"),
        (ok(p8, "spool_respects_backoff"), "恢复后退避状态保留"),
        (ok(p8, "spool_attempt_after_backoff"), "恢复后可继续投递"),
        (ok(p8, "recovered_report_ok"), "恢复后报表精确"),
    ]))
    # Probe-phase exceptions are ERROR evidence, not model failures: surface
    # them on the milestone so a judge can tell "crashed" from "wrong".
    _phase_of = {"m1": "p1", "m2": "p1", "m3": "p2", "m4": "p7", "m5": "p3",
                 "m6": "p4", "m7": "p4", "m8": "p5", "m9": "p6", "m10": "p6",
                 "m11": "p8"}
    for m in ms:
        phase = _phase_of.get(m.milestone_id.rsplit("_", 1)[-1])
        if phase in _errored:
            m.failure_reason = (
                "PROBE-ERROR: %s" % str(data[phase].get("error"))[:160])
    return ms


# ---------------------------------------------------------------------------
# Mutants + self-test: the checker must DISCRIMINATE, not just pass the oracle
# ---------------------------------------------------------------------------

_MUTANTS: dict[str, tuple[str, str]] = {
    # name: (anchor in REFERENCE_FULFILLMENT, replacement)
    "no_idempotency": (
        """            known = self._by_idem.get(idem_key)
            if known is not None:
                return self._view(known)
""",
        ""),
    "no_state_guard_pay": (
        """            if o["state"] != _RESERVED:
                raise ValueError("order is not RESERVED")
""",
        ""),
    "no_compensation": (
        """            o["state"] = _PAID if ok else _CANCELLED""",
        """            o["state"] = _PAID if ok else _PAID"""),
    "no_ttl_sweep": (
        """                if o.get("reserved_at") is not None and now >= o["reserved_at"] + o.get("ttl", 0.0):""",
        """                if False:"""),
    "no_backoff": (
        """                if last is None or now >= last + self.delivery_backoff_seconds:""",
        """                if True:"""),
    "no_dlq": (
        """            elif due["attempts"] >= self.delivery_max_attempts:
                del self._spool[due["order_id"]]
                self._dlq[due["order_id"]] = entry""",
        """            elif False:
                pass"""),
    "no_journal": (
        """    def _flush(self):
        if not self.journal_path:
            return""",
        """    def _flush(self):
        if True:
            return"""),
    "oversell_api": (
        """            for sku, qty in need.items():
                if report.get(sku, {}).get("available", 0) < qty:
                    raise ValueError("insufficient stock for %s" % sku)""",
        """            for sku, qty in need.items():
                if report.get(sku, {}).get("available", 0) < qty:
                    pass  # allow oversell through the API"""),
}

#: each mutant must knock down at least one of these milestones
_MUTANT_EXPECT: dict[str, str] = {
    "no_idempotency": "m2",
    "no_state_guard_pay": "m7",
    "no_compensation": "m7",
    "no_ttl_sweep": "m5",
    "no_backoff": "m9",
    "no_dlq": "m9",
    "no_journal": "m11",
    "oversell_api": "m3",
}


def self_test() -> tuple[int, int]:
    counts = [0, 0]

    def check(name: str, cond: bool) -> None:
        counts[0 if cond else 1] += 1
        print(f"{'PASS' if cond else 'FAIL'} business::{name}", flush=True)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="biz-selftest-") as td:
        # 1) oracle: reference must score a clean 11/11
        Path(td, "fulfillment.py").write_text(
            REFERENCE_FULFILLMENT, encoding="utf-8")
        try:
            data = run_fulfillment_scenario(td)
            ms = build_fulfillment_milestones(data)
            mean = sum(m.score for m in ms) / len(ms)
            check("oracle_full_marks", mean >= 0.999 and len(ms) == 11)
        except Exception as exc:  # noqa: BLE001
            check(f"oracle_raises::{type(exc).__name__}", False)
            return counts[0], counts[1]

        # 2) mutants: each must lose points on its targeted milestone
        for name, (anchor, repl) in _MUTANTS.items():
            mutant = REFERENCE_FULFILLMENT.replace(anchor, repl, 1)
            if mutant == REFERENCE_FULFILLMENT:
                check(f"mutant_{name}_anchor_applied", False)
                continue
            mdir = Path(td) / ("mut_" + name)
            mdir.mkdir()
            (mdir / "fulfillment.py").write_text(mutant, encoding="utf-8")
            try:
                mdata = run_fulfillment_scenario(mdir)
                mms = build_fulfillment_milestones(mdata)
                want = _MUTANT_EXPECT[name]
                hit = [m for m in mms if m.milestone_id.endswith(want)]
                knocked = bool(hit) and hit[0].score < 0.999
                check(f"mutant_{name}_lowers_{want}", knocked)
            except Exception as exc:  # noqa: BLE001
                check(f"mutant_{name}_raises::{type(exc).__name__}", False)

        # 3) missing deliverable fails closed
        empty = Path(td) / "empty"
        empty.mkdir()
        try:
            data = run_fulfillment_scenario(empty)
            # subprocess isolation: the child reports the missing module as
            # a fatal payload; milestones must then all score zero.
            check("missing_file_fails_closed",
                  isinstance(data, dict) and "fatal" in data
                  and all(m.score == 0.0
                          for m in build_fulfillment_milestones(data)))
        except FileNotFoundError:
            check("missing_file_fails_closed", True)

    return counts[0], counts[1]


if __name__ == "__main__":
    if "--worker" in sys.argv:
        # child mode: run the scenario in-process and dump evidence as JSON
        _ws = sys.argv[sys.argv.index("--worker") + 1]
        print(json.dumps(_scenario_worker(_ws), ensure_ascii=False))
        raise SystemExit(0)
    if "--self-test" in sys.argv:
        _p, _f = self_test()
        print(f"business_task self-test: {_p} passed, {_f} failed", flush=True)
        raise SystemExit(0 if _f == 0 else 1)
