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




#: ledger mutants: (anchor in REFERENCE_LEDGER, replacement)
_LEDGER_MUTANTS: dict[str, tuple[str, str]] = {
    "no_balance_check": (
        """            if self.balance(from_account) < amount:
                raise ValueError("insufficient funds")""",
        """            pass  # no balance check"""),
    "no_idempotency": (
        """            prior = self._by_idem.get(idem_key)
            if prior is not None:
                return self._view(prior)
            legs = self._validate_legs(entries)""",
        """            legs = self._validate_legs(entries)"""),
    "no_unbalanced_check": (
        """        if sum(l["debit"] for l in legs) != sum(l["credit"] for l in legs):
            raise ValueError("unbalanced posting")""",
        """        pass"""),
    "no_currency_check": (
        """        if len({self._accounts[l["account"]] for l in legs}) > 1:
            raise ValueError("mixed currencies in one posting")""",
        """        pass"""),
    "no_double_reverse_guard": (
        """            if original_idem_key in self._reversed:
                raise ValueError("already reversed")""",
        """            pass"""),
    "no_settle_idem": (
        """            if day in self._settled:
                return self._settlement_batch(self._settled[day])""",
        """            pass"""),
    "no_duplicate_detect": (
        """            for key, n in sorted(seen.items()):
                if n > 1:
                    reports.append({"kind": "duplicate_posting",
                                    "batch": key[0], "seq": key[1],
                                    "account": key[2], "copies": n})""",
        """            pass"""),
    "order_sensitive_reconcile": (
        """            have = {(str(i.get("batch")), int(i.get("seq", 0)),
                     str(i.get("account")), int(i.get("debit", 0) or 0),
                     int(i.get("credit", 0) or 0))
                    for i in stream}""",
        """            have = {(str(i.get("batch")), int(i.get("seq", 0)),
                     str(i.get("account")), int(i.get("debit", 0) or 0),
                     int(i.get("credit", 0) or 0))
                    for i in stream[:1]}"""),
    "no_journal": (
        """    def _flush(self):
        if not self.journal_path:
            return""",
        """    def _flush(self):
        if True:
            return"""),
    # NOTE: a check-then-act transfer (balance check outside the lock) is a
    # real bug but is NOT reliably observable here — CPython's GIL gives the
    # window between the inner balance() lock release and the re-acquire no
    # chance to yield, so even 8x16-thread contention did not trigger it in
    # validation. m3 stays as a safety gate; that particular mutant is not
    # gated. Deterministic locking mutants (no balance check, no journal)
    # are covered by other rows.
}

#: each ledger mutant must knock down at least one of these milestones
_LEDGER_MUTANT_EXPECT: dict[str, str] = {
    "no_balance_check": "m2",
    "no_idempotency": "m4",
    "no_unbalanced_check": "m1",
    "no_currency_check": "m1",
    "no_double_reverse_guard": "m5",
    "no_settle_idem": "m6",
    "no_duplicate_detect": "m7",
    "order_sensitive_reconcile": "m8",
    "no_journal": "m9",
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

        # 4) ledger oracle: reference must score a clean 9/9
        Path(td, "ledger.py").write_text(
            REFERENCE_LEDGER, encoding="utf-8")
        try:
            ldata = run_ledger_scenario_isolated(td)
            lms = build_ledger_milestones(ldata)
            lmean = sum(m.score for m in lms) / len(lms)
            check("ledger_oracle_full_marks", lmean >= 0.999 and len(lms) == 9)
        except Exception as exc:  # noqa: BLE001
            check(f"ledger_oracle_raises::{type(exc).__name__}", False)
            return counts[0], counts[1]

        # 5) ledger mutants: each must lose points on its targeted milestone
        for name, (a_anchor, a_repl) in _LEDGER_MUTANTS.items():
            mutant = REFERENCE_LEDGER.replace(a_anchor, a_repl, 1)
            if mutant == REFERENCE_LEDGER:
                check(f"ledger_mutant_{name}_anchor_applied", False)
                continue
            mdir = Path(td) / ("lmut_" + name)
            mdir.mkdir()
            (mdir / "ledger.py").write_text(mutant, encoding="utf-8")
            try:
                mdata = run_ledger_scenario_isolated(mdir)
                mms = build_ledger_milestones(mdata)
                want = _LEDGER_MUTANT_EXPECT[name]
                hit = [m for m in mms if m.milestone_id.endswith(want)]
                knocked = bool(hit) and hit[0].score < 0.999
                check(f"ledger_mutant_{name}_lowers_{want}", knocked)
            except Exception as exc:  # noqa: BLE001
                check(f"ledger_mutant_{name}_raises::{type(exc).__name__}", False)

        # 6) missing ledger deliverable fails closed via the isolated wrapper
        lempty = Path(td) / "lempty"
        lempty.mkdir()
        ldata = run_ledger_scenario_isolated(lempty)
        check("ledger_missing_file_fails_closed",
              isinstance(ldata, dict) and "fatal" in ldata
              and all(m.score == 0.0 for m in build_ledger_milestones(ldata)))

    return counts[0], counts[1]





# ---------------------------------------------------------------------------
# Task 2: payment_ledger — contract (what the model reads as TASK.md)
# ---------------------------------------------------------------------------

LEDGER_TITLE = "Payment Ledger & Daily Settlement"

LEDGER_CONTRACT = """\
# Payment Ledger & Daily Settlement

You are implementing the money-movement core of a payments platform. Every
value that moves does so as a balanced double-entry journal posting; the
ledger must stay internally consistent under concurrent transfers, survive
restarts, support reversals and partial refunds, close each day with an
idempotent settlement batch, and reconcile an incoming entry stream against
its own books — catching missing entries, unbalanced batches and duplicate
postings while never crying wolf on a clean stream.

Implement everything in ONE file: `ledger.py` (standard library only, no
network, no third-party packages). The grader imports your module and drives
the API below with an explicit clock. Every mutation must be durable: the
process may be killed at any point and restarted with the same journal path.

## Required API

```python
class LedgerService:
    def __init__(self, journal_path: str | None = None) -> None: ...

    # -- accounts ---------------------------------------------------------
    def open_account(self, account_id: str, currency: str = "USD") -> None
        # Register an account. Unknown accounts must be rejected everywhere.

    def balance(self, account_id: str) -> int
        # Current balance in minor units (cents). Negative balances are
        # impossible: transfers that would overdraw are rejected atomically.

    def trial_balance(self) -> dict[str, tuple[int, int]]
        # {currency: (total_debits, total_cents)} — the two totals of each
        # currency must ALWAYS be equal (the core double-entry invariant).

    # -- postings -----------------------------------------------------------
    def post(self, idem_key: str, entries: list[dict], now: float,
             day: str) -> dict
        # entries: [{"account": str, "debit": int}, ...] and/or
        #          [{"account": str, "credit": int}, ...]
        # Reject (ValueError) when: entries is empty, any amount <= 0, any
        # account is unknown, or the posting is unbalanced
        # (sum(debits) != sum(credits)). A replay of the SAME idem_key
        # returns the recorded result without posting anything again.

    def transfer(self, idem_key: str, from_account: str, to_account: str,
                 amount: int, now: float, day: str) -> dict
        # Atomic two-leg posting (debit source, credit destination).
        # Reject when amount <= 0, accounts differ in currency, the source
        # would overdraw, or the idem_key was already used. Replays return
        # the recorded outcome.

    def reverse(self, idem_key: str, original_idem_key: str, now: float,
                day: str) -> dict
        # Post the exact inverse of a previous posting (debits become
        # credits and vice versa) as a NEW journal entry — history is never
        # rewritten or deleted. Replays of `idem_key` return the recorded
        # reversal. Reversing an unknown key, or reversing a reversal, is a
        # ValueError.

    # -- settlement ----------------------------------------------------------
    def settle(self, day: str, now: float) -> dict
        # Close the books for `day`: compute each account's net movement
        # for entries stamped with that day, emit ONE settlement batch
        # (a balanced posting carrying the per-account nets), and mark the
        # day settled. Re-running settle for the same day is a no-op that
        # returns the recorded batch. Returns
        # {"day": str, "batch_id": str, "nets": {account: int}}.

    # -- reconciliation --------------------------------------------------------
    def reconcile(self, stream: list[dict]) -> list[dict]
        # Compare an incoming entry stream against this ledger's own
        # postings and report discrepancies. Each stream item:
        #   {"seq": int, "account": str, "debit": int, "credit": int,
        #    "batch": str}
        # Items may arrive OUT OF ORDER (seq is the truth, not list order).
        # Report a list of discrepancy dicts:
        #   {"kind": "missing_entry", "seq": int, "account": str, ...}
        #   {"kind": "unbalanced_batch", "batch": str, ...}
        #   {"kind": "duplicate_posting", "seq": int, ...}
        # A stream that faithfully mirrors this ledger's postings (even
        # shuffled) must produce ZERO reports.
```

## Journal entry dict (your internal shape)

```python
{"entry_id": str, "idem_key": str, "day": str, "seq": int,
 "legs": [{"account": str, "debit": int, "credit": int}], "ts": float}
```

## Durability

`__init__(journal_path)` must load prior state (accounts, entries, idem
index, settlement state) when the file exists, and every mutating call must
keep the journal current on disk (atomic write). A fresh service with the
same journal_path resumes exactly — no lost entries, no reused idem keys,
no duplicated settlement batches. When `journal_path` is None the service
runs purely in memory.

## Rules

- Standard library only; the grader calls the API from up to 16 threads at
  once (concurrent transfers on shared accounts must be safe and conserve
  value exactly) and drives the clock explicitly via `now`.
- `day` is an opaque string (e.g. "2026-09-24") stamped on every posting
  (post/transfer/reverse all take it); `seq` is a monotonically increasing
  per-posting counter assigned by you.
- bash/`python -c` prototypes are not scored; only `ledger.py` matters.
- When the file is ready, call the `finish` tool with a summary of what you
  implemented and how you checked it.
"""


# ---------------------------------------------------------------------------
# Reference implementation for payment_ledger (self-test oracle)
# ---------------------------------------------------------------------------

REFERENCE_LEDGER = '''
"""Payment ledger & daily settlement (reference, stdlib only)."""
from __future__ import annotations

import json
import os
import threading


class LedgerService:
    def __init__(self, journal_path=None):
        self.journal_path = journal_path
        self._lock = threading.RLock()
        self._accounts = {}        # account_id -> currency
        self._entries = []         # journal entries (dicts)
        self._by_idem = {}         # idem_key -> entry_id
        self._batch_of = {}        # entry_id -> batch (idem_key of the posting)
        self._reversed = set()     # idem_keys that have been reversed
        self._settled = {}         # day -> batch entry_id (or None if empty)
        self._counter = 0
        if journal_path:
            try:
                with open(journal_path, "r", encoding="utf-8") as fh:
                    self._load(json.load(fh))
            except FileNotFoundError:
                pass

    # -- persistence -------------------------------------------------------
    def _snapshot(self):
        return {"accounts": self._accounts, "entries": self._entries,
                "by_idem": self._by_idem, "batch_of": self._batch_of,
                "reversed": sorted(self._reversed), "settled": self._settled,
                "counter": self._counter}

    def _load(self, state):
        self._accounts = dict(state.get("accounts") or {})
        self._entries = list(state.get("entries") or [])
        self._by_idem = dict(state.get("by_idem") or {})
        self._batch_of = dict(state.get("batch_of") or {})
        self._reversed = set(state.get("reversed") or [])
        self._settled = dict(state.get("settled") or {})
        self._counter = int(state.get("counter") or 0)

    def _flush(self):
        if not self.journal_path:
            return
        tmp = self.journal_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._snapshot(), fh)
        os.replace(tmp, self.journal_path)

    # -- accounts ------------------------------------------------------------
    def open_account(self, account_id, currency="USD"):
        with self._lock:
            if account_id in self._accounts:
                return
            self._accounts[account_id] = str(currency)
            self._flush()

    def balance(self, account_id):
        with self._lock:
            if account_id not in self._accounts:
                raise ValueError("unknown account")
            total = 0
            for e in self._entries:
                for leg in e["legs"]:
                    if leg["account"] == account_id:
                        total += int(leg["debit"]) - int(leg["credit"])
            return total

    def trial_balance(self):
        with self._lock:
            out = {}
            for e in self._entries:
                for leg in e["legs"]:
                    cur = self._accounts[leg["account"]]
                    d, c = out.get(cur, (0, 0))
                    out[cur] = (d + int(leg["debit"]), c + int(leg["credit"]))
            return out

    # -- postings ---------------------------------------------------------------
    def _apply_legs(self, legs, day, ts):
        self._counter += 1
        entry = {"entry_id": "e%06d" % self._counter, "idem_key": None,
                 "day": day, "seq": self._counter,
                 "legs": [dict(l) for l in legs], "ts": ts}
        self._entries.append(entry)
        return entry

    def _validate_legs(self, entries):
        if not entries:
            raise ValueError("empty posting")
        legs = []
        for item in entries:
            account = str(item["account"])
            debit = int(item.get("debit", 0) or 0)
            credit = int(item.get("credit", 0) or 0)
            if debit < 0 or credit < 0:
                raise ValueError("negative amount")
            if debit and credit:
                raise ValueError("a leg is either debit or credit")
            if not debit and not credit:
                raise ValueError("zero amount")
            if account not in self._accounts:
                raise ValueError("unknown account: %s" % account)
            legs.append({"account": account, "debit": debit, "credit": credit})
        if sum(l["debit"] for l in legs) != sum(l["credit"] for l in legs):
            raise ValueError("unbalanced posting")
        if len({self._accounts[l["account"]] for l in legs}) > 1:
            raise ValueError("mixed currencies in one posting")
        return legs

    def _record(self, entry, idem_key):
        entry["idem_key"] = idem_key
        self._by_idem[idem_key] = entry["entry_id"]
        self._batch_of[entry["entry_id"]] = idem_key
        self._flush()

    def _view(self, entry_id):
        e = next(x for x in self._entries if x["entry_id"] == entry_id)
        return {"entry_id": e["entry_id"], "idem_key": e["idem_key"],
                "day": e["day"], "seq": e["seq"],
                "legs": [dict(l) for l in e["legs"]], "ts": e["ts"]}

    def post(self, idem_key, entries, now, day=None):
        with self._lock:
            prior = self._by_idem.get(idem_key)
            if prior is not None:
                return self._view(prior)
            legs = self._validate_legs(entries)
            entry = self._apply_legs(legs, day, now)
            self._record(entry, idem_key)
            return self._view(entry["entry_id"])

    def transfer(self, idem_key, from_account, to_account, amount, now, day=None):
        with self._lock:
            prior = self._by_idem.get(idem_key)
            if prior is not None:
                return self._view(prior)
            amount = int(amount)
            if amount <= 0:
                raise ValueError("amount must be positive")
            for acc in (from_account, to_account):
                if acc not in self._accounts:
                    raise ValueError("unknown account")
            if self._accounts[from_account] != self._accounts[to_account]:
                raise ValueError("currency mismatch")
            if self.balance(from_account) < amount:
                raise ValueError("insufficient funds")
            # from pays to: from loses `amount` (credit), to gains (debit).
            legs = [{"account": from_account, "debit": 0, "credit": amount},
                    {"account": to_account, "debit": amount, "credit": 0}]
            entry = self._apply_legs(legs, day, now)
            self._record(entry, idem_key)
            return self._view(entry["entry_id"])

    def reverse(self, idem_key, original_idem_key, now, day=None):
        with self._lock:
            prior = self._by_idem.get(idem_key)
            if prior is not None:
                return self._view(prior)
            if original_idem_key not in self._by_idem:
                raise ValueError("unknown original key")
            if original_idem_key in self._reversed:
                raise ValueError("already reversed")
            orig = self._view(self._by_idem[original_idem_key])
            legs = [{"account": l["account"], "debit": l["credit"],
                     "credit": l["debit"]} for l in orig["legs"]]
            entry = self._apply_legs(legs, day, now)
            self._record(entry, idem_key)
            self._reversed.add(original_idem_key)
            return self._view(entry["entry_id"])

    # -- settlement ---------------------------------------------------------------
    def settle(self, day, now):
        with self._lock:
            if day in self._settled:
                return self._settlement_batch(self._settled[day])
            nets = {}
            for e in self._entries:
                if e.get("day") != day:
                    continue
                for leg in e["legs"]:
                    nets[leg["account"]] = (nets.get(leg["account"], 0)
                                            + int(leg["debit"]) - int(leg["credit"]))
            nets = {a: n for a, n in nets.items() if n}
            legs = []
            for account, net in sorted(nets.items()):
                if net > 0:
                    legs.append({"account": account, "debit": net, "credit": 0})
                else:
                    legs.append({"account": account, "debit": 0, "credit": -net})
            if legs:
                entry = self._apply_legs(legs, day, now)
                self._record(entry, "settle-%s" % day)
                self._settled[day] = entry["entry_id"]
            else:
                self._settled[day] = None
            self._flush()
            return {"day": day, "batch_id": self._settled[day], "nets": nets}

    def _settlement_batch(self, entry_id):
        if entry_id is None:
            return {"day": None, "batch_id": None, "nets": {}}
        e = self._view(entry_id)
        nets = {l["account"]: int(l["debit"]) - int(l["credit"])
                for l in e["legs"]}
        return {"day": e["day"], "batch_id": e["entry_id"], "nets": nets}

    # -- reconciliation -------------------------------------------------------------
    def reconcile(self, stream):
        with self._lock:
            reports = []
            stream = [s for s in (stream or []) if isinstance(s, dict)]
            # 1) duplicate legs in the stream
            seen = {}
            for item in stream:
                key = (str(item.get("batch")), int(item.get("seq", 0)),
                       str(item.get("account")))
                seen[key] = seen.get(key, 0) + 1
            for key, n in sorted(seen.items()):
                if n > 1:
                    reports.append({"kind": "duplicate_posting",
                                    "batch": key[0], "seq": key[1],
                                    "account": key[2], "copies": n})
            # 2) unbalanced batches in the stream
            per_batch = {}
            for item in stream:
                per_batch.setdefault(str(item.get("batch")), []).append(item)
            for batch, items in sorted(per_batch.items()):
                d = sum(int(i.get("debit", 0) or 0) for i in items)
                c = sum(int(i.get("credit", 0) or 0) for i in items)
                if d != c:
                    reports.append({"kind": "unbalanced_batch", "batch": batch,
                                    "debits": d, "credits": c})
            # 3) entries the ledger holds but the stream lacks (seq is truth:
            #    list order never matters)
            have = {(str(i.get("batch")), int(i.get("seq", 0)),
                     str(i.get("account")), int(i.get("debit", 0) or 0),
                     int(i.get("credit", 0) or 0))
                    for i in stream}
            for e in self._entries:
                batch = self._batch_of.get(e["entry_id"], e.get("idem_key"))
                for leg in e["legs"]:
                    key = (str(batch), int(e["seq"]), str(leg["account"]),
                           int(leg["debit"]), int(leg["credit"]))
                    if key not in have:
                        reports.append({"kind": "missing_entry",
                                        "batch": key[0], "seq": key[1],
                                        "account": key[2]})
            return reports
'''

# ---------------------------------------------------------------------------
# Ledger scenario harness: drives the model's LedgerService through probes
# ---------------------------------------------------------------------------

def _load_ledger_class(workspace_dir: str | Path):
    """Import the model's ledger.py and return LedgerService."""
    path = Path(workspace_dir) / "ledger.py"
    if not path.is_file():
        raise FileNotFoundError("ledger.py missing")
    spec = importlib.util.spec_from_file_location("model_ledger", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.LedgerService


_LEDGER_RESTART_RUNNER = '''"""Restart-probe child: drives the model ledger, then dies hard."""
import importlib.util
import json
import os
import sys

module_path, journal_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

spec = importlib.util.spec_from_file_location("model_ledger", module_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

svc = mod.LedgerService(journal_path=journal_path)
svc.open_account("A", "USD")
svc.open_account("B", "USD")
svc.open_account("C", "USD")
svc.post("p1", [{"account": "A", "debit": 500},
                {"account": "B", "credit": 500}], now=1.0, day="D1")
svc.transfer("t1", "A", "C", 120, now=2.0, day="D1")
batch = svc.settle("D1", now=3.0)
# kill hard: no flush hooks, no atexit — whatever is on disk is what counts
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({"batch_id": batch.get("batch_id")}, fh)
os._exit(9)
'''


def _run_ledger_restart_probe(workspace_dir: str | Path) -> dict[str, Any]:
    """Phase 1 in a hard-killed child; phase 2 re-opens the same journal."""
    with tempfile.TemporaryDirectory(prefix="ledger-restart-") as tmp:
        module_path = str(Path(workspace_dir) / "ledger.py")
        journal = str(Path(tmp) / "journal.json")
        runner = Path(tmp) / "restart_runner.py"
        runner.write_text(_LEDGER_RESTART_RUNNER, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(runner), module_path, journal, str(Path(tmp) / "out.json")],
            capture_output=True, text=True, timeout=60)
        recovered: dict[str, Any] = {"killed": proc.returncode != 0}
        if not Path(journal).is_file():
            recovered["journal_missing"] = True
            return recovered
        try:
            LedgerService = _load_ledger_class(workspace_dir)
            svc = LedgerService(journal_path=journal)
        except Exception as exc:  # noqa: BLE001
            recovered["reload_error"] = repr(exc)
            return recovered

        recovered["A"] = svc.balance("A")
        recovered["B"] = svc.balance("B")
        recovered["C"] = svc.balance("C")
        tb = svc.trial_balance().get("USD")
        recovered["trial_balanced"] = bool(tb) and tb[0] == tb[1]
        # idempotent replay after recovery: no new entry, balances unchanged
        try:
            _before = svc.balance("A")
            r = svc.post("p1", [{"account": "A", "debit": 500},
                                {"account": "B", "credit": 500}],
                         now=9.0, day="D1")
            recovered["replay_ok"] = (svc.balance("A") == _before and bool(r))
        except Exception as exc:  # noqa: BLE001
            recovered["replay_error"] = repr(exc)
        # settlement must not double after recovery
        try:
            b2 = svc.settle("D1", now=10.0)
            out_file = Path(tmp) / "out.json"
            batch_id = None
            if out_file.is_file():
                batch_id = json.loads(out_file.read_text(encoding="utf-8")).get("batch_id")
            recovered["settle_not_doubled"] = (
                batch_id is not None and b2.get("batch_id") == batch_id)
        except Exception as exc:  # noqa: BLE001
            recovered["settle_error"] = repr(exc)
        return recovered


def run_ledger_scenario(workspace_dir: str | Path) -> dict[str, Any]:
    """Drive the model's service through every probe; return raw evidence."""
    out: dict[str, Any] = {}
    LedgerService = _load_ledger_class(workspace_dir)
    svc = LedgerService()

    # -- P1: postings, balance invariant, validation --------------------------
    p1: dict[str, Any] = {}
    try:
        for acc in ("A", "B", "C"):
            svc.open_account(acc, "USD")
        svc.open_account("E", "EUR")
        ok = svc.post("p1", [{"account": "A", "debit": 100},
                             {"account": "B", "credit": 100}], now=1.0, day="D1")
        p1["posted"] = bool(ok)
        p1["balances"] = {"A": svc.balance("A"), "B": svc.balance("B")}
        tb = svc.trial_balance()
        p1["trial_balanced"] = tb.get("USD", (0, 1))[0] == tb.get("USD", (1, 0))[1]
        rejects = {}
        for name, fn in (
            ("unbalanced", lambda: svc.post("bad1", [{"account": "A", "debit": 50}], now=1.0, day="D1")),
            ("empty", lambda: svc.post("bad2", [], now=1.0, day="D1")),
            ("zero_amount", lambda: svc.post("bad3", [{"account": "A", "debit": 0}], now=1.0, day="D1")),
            ("negative", lambda: svc.post("bad4", [{"account": "A", "debit": -5}], now=1.0, day="D1")),
            ("unknown_account", lambda: svc.post("bad5", [{"account": "ZZ", "debit": 5}], now=1.0, day="D1")),
            ("mixed_currency", lambda: svc.post("bad6", [{"account": "A", "debit": 5}, {"account": "E", "credit": 5}], now=1.0, day="D1")),
        ):
            try:
                fn()
                rejects[name] = False
            except ValueError:
                rejects[name] = True
            except Exception as exc:  # noqa: BLE001
                rejects[name] = "wrong-exc:%s" % type(exc).__name__
        p1["rejects"] = rejects
        p1["balances_unchanged"] = svc.balance("A") == 100
    except Exception as exc:  # noqa: BLE001
        p1["error"] = repr(exc)
    out["p1"] = p1

    # -- P2: transfer atomicity -------------------------------------------------
    p2: dict[str, Any] = {}
    try:
        t = svc.transfer("t1", "A", "B", 30, now=2.0, day="D1")
        p2["transferred"] = (svc.balance("A") == 70 and svc.balance("B") == -70)
        checks = {}
        try:
            svc.transfer("t2", "B", "A", 999999, now=2.0, day="D1")
            checks["insufficient_rejected"] = False
        except ValueError:
            checks["insufficient_rejected"] = True
        checks["no_partial"] = svc.balance("B") == -70
        try:
            svc.transfer("t3", "A", "E", 5, now=2.0, day="D1")
            checks["currency_mismatch_rejected"] = False
        except ValueError:
            checks["currency_mismatch_rejected"] = True
        try:
            svc.transfer("t4", "A", "ZZ", 5, now=2.0, day="D1")
            checks["unknown_account_rejected"] = False
        except ValueError:
            checks["unknown_account_rejected"] = True
        try:
            svc.transfer("t5", "A", "B", 0, now=2.0, day="D1")
            checks["zero_amount_rejected"] = False
        except ValueError:
            checks["zero_amount_rejected"] = True
        p2.update(checks)
        p2["balances_exact"] = (svc.balance("A") == 70 and svc.balance("B") == -70)
    except Exception as exc:  # noqa: BLE001
        p2["error"] = repr(exc)
    out["p2"] = p2

    # -- P3: 16-thread concurrency conservation ---------------------------------
    p3: dict[str, Any] = {}
    try:
        svc3 = LedgerService()
        n = 16
        for i in range(n):
            svc3.open_account("acc%d" % i, "USD")
        svc3.open_account("sink", "USD")
        svc3.open_account("pool", "USD")
        # route funds into each account: pool -> acc_i 100 each
        for i in range(n):
            svc3.post("fund%d" % i, [{"account": "acc%d" % i, "debit": 100},
                                     {"account": "pool", "credit": 100}],
                      now=0.0, day="D0")
        barrier = threading.Barrier(n)
        results: list[str] = []
        res_lock = threading.Lock()

        def _move(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                svc3.transfer("mv%d" % i, "acc%d" % i, "sink", 10, now=1.0, day="D1")
                svc3.transfer("bk%d" % i, "sink", "acc%d" % i, 10, now=2.0, day="D1")
                with res_lock:
                    results.append("ok")
            except Exception as exc:  # noqa: BLE001
                with res_lock:
                    results.append("err:%s" % type(exc).__name__)

        threads = [threading.Thread(target=_move, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        p3["all_ok"] = results.count("ok") == n
        p3["no_crash"] = not any(r.startswith("err:") for r in results)
        p3["accounts_restored"] = all(
            svc3.balance("acc%d" % i) == 100 for i in range(n))
        p3["sink_zero"] = svc3.balance("sink") == 0
        tb = svc3.trial_balance().get("USD", (0, 1))
        p3["trial_balanced"] = tb[0] == tb[1]
        # contention: 16 threads drain one funded account exactly
        svc4 = LedgerService()
        svc4.open_account("cold", "USD")
        svc4.open_account("hot0", "USD")
        for i in range(1, n + 1):
            svc4.open_account("hot%d" % i, "USD")
        # 100 on hand, 16 contenders x 10 -> exactly 10 must succeed; a
        # check-then-act race lets more through and overdraws the account.
        svc4.post("load", [{"account": "hot0", "debit": 100},
                           {"account": "cold", "credit": 100}], now=0.0, day="D0")
        barrier2 = threading.Barrier(n)
        drained: list[str] = []

        def _drain(i: int) -> None:
            try:
                barrier2.wait(timeout=10)
                svc4.transfer("dr%d" % i, "hot0", "hot%d" % (i + 1), 10, now=1.0, day="D1")
                with res_lock:
                    drained.append("ok")
            except ValueError:
                with res_lock:
                    drained.append("rejected")
            except Exception as exc:  # noqa: BLE001
                with res_lock:
                    drained.append("err:%s" % type(exc).__name__)

        threads = [threading.Thread(target=_drain, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        p3["drain_exactly_10"] = drained.count("ok") == 10
        p3["drain_rejected_6"] = drained.count("rejected") == n - 10
        p3["drain_no_crash"] = not any(r.startswith("err:") for r in drained)
        p3["drain_exact_balances"] = (
            svc4.balance("hot0") == 0 and svc4.balance("cold") == -100)
        p3["no_negative"] = all(
            svc4.balance("hot%d" % i) >= 0 for i in range(1, n + 1))
    except Exception as exc:  # noqa: BLE001
        p3["error"] = repr(exc)
    out["p3"] = p3

    # -- P4: idempotent replay ---------------------------------------------------
    p4: dict[str, Any] = {}
    try:
        svc4b = LedgerService()
        svc4b.open_account("X", "USD")
        svc4b.open_account("Y", "USD")
        svc4b.post("q1", [{"account": "X", "debit": 100},
                          {"account": "Y", "credit": 100}], now=1.0, day="D1")
        r1 = svc4b.post("q1", [{"account": "X", "debit": 100},
                               {"account": "Y", "credit": 100}], now=2.0, day="D1")
        p4["post_replay_same"] = bool(r1) and r1.get("entry_id") is not None
        p4["post_replay_no_double"] = (svc4b.balance("X") == 100
                                       and svc4b.balance("Y") == -100)
        svc4b.transfer("q2", "X", "Y", 40, now=3.0, day="D1")
        r2 = svc4b.transfer("q2", "X", "Y", 40, now=4.0, day="D1")
        p4["transfer_replay_same"] = bool(r2)
        p4["transfer_replay_no_double"] = (svc4b.balance("X") == 60
                                           and svc4b.balance("Y") == -60)
        tb = svc4b.trial_balance().get("USD", (0, 1))
        p4["trial_balanced"] = tb[0] == tb[1]
    except Exception as exc:  # noqa: BLE001
        p4["error"] = repr(exc)
    out["p4"] = p4

    # -- P5: reversals -------------------------------------------------------------
    p5: dict[str, Any] = {}
    try:
        svc5 = LedgerService()
        svc5.open_account("A", "USD")
        svc5.open_account("B", "USD")
        svc5.post("r0", [{"account": "A", "debit": 100},
                         {"account": "B", "credit": 100}], now=1.0, day="D1")
        svc5.reverse("rv1", "r0", now=2.0, day="D1")
        p5["reversed_balances"] = (svc5.balance("A") == 0 and svc5.balance("B") == 0)
        try:
            svc5.reverse("rv2", "r0", now=3.0, day="D1")
            p5["double_reverse_rejected"] = False
        except ValueError:
            p5["double_reverse_rejected"] = True
        try:
            svc5.reverse("rv3", "nope", now=3.0, day="D1")
            p5["unknown_original_rejected"] = False
        except ValueError:
            p5["unknown_original_rejected"] = True
        # partial refund is a fresh balanced posting; history is never deleted
        svc5.post("r1", [{"account": "A", "debit": 100},
                         {"account": "B", "credit": 100}], now=4.0, day="D1")
        svc5.post("r2", [{"account": "B", "debit": 30},
                         {"account": "A", "credit": 30}], now=5.0, day="D1")
        p5["partial_refund"] = (svc5.balance("A") == 70 and svc5.balance("B") == -70)
        tb = svc5.trial_balance().get("USD", (0, 1))
        p5["trial_balanced"] = tb[0] == tb[1]
        p5["replay_reverse"] = None
    except Exception as exc:  # noqa: BLE001
        p5["error"] = repr(exc)
    out["p5"] = p5

    # -- P6: settlement --------------------------------------------------------------
    p6: dict[str, Any] = {}
    try:
        svc6 = LedgerService()
        for acc in ("A", "B", "C"):
            svc6.open_account(acc, "USD")
        svc6.post("s1", [{"account": "A", "credit": 100},
                         {"account": "B", "debit": 100}], now=1.0, day="D1")
        svc6.transfer("s2", "B", "C", 30, now=2.0, day="D1")
        b1 = svc6.settle("D1", now=3.0)
        p6["nets_correct"] = b1.get("nets") == {"A": -100, "B": 70, "C": 30}
        p6["batch_id_present"] = bool(b1.get("batch_id"))
        tb = svc6.trial_balance().get("USD", (0, 1))
        p6["trial_balanced_after_settle"] = tb[0] == tb[1]
        b2 = svc6.settle("D1", now=4.0)
        p6["resettle_same_batch"] = b2.get("batch_id") == b1.get("batch_id")
        p6["resettle_same_nets"] = b2.get("nets") == b1.get("nets")
        # a different day settles independently
        svc6.transfer("s3", "C", "A", 10, now=5.0, day="D2")
        b3 = svc6.settle("D2", now=6.0)
        p6["second_day_nets"] = b3.get("nets") == {"A": 10, "C": -10}
    except Exception as exc:  # noqa: BLE001
        p6["error"] = repr(exc)
    out["p6"] = p6

    # -- P7/P8: reconciliation (planted + clean + shuffled) ---------------------------
    p7: dict[str, Any] = {}
    try:
        svc7 = LedgerService()
        for acc in ("A", "B", "C"):
            svc7.open_account(acc, "USD")
        e1 = svc7.post("k1", [{"account": "A", "debit": 100},
                              {"account": "B", "credit": 100}], now=1.0, day="D1")
        e2 = svc7.transfer("k2", "A", "C", 40, now=2.0, day="D1")

        def _mirror(entries):
            items = []
            for e in entries:
                batch = e.get("idem_key")
                for leg in e["legs"]:
                    items.append({"seq": e["seq"], "account": leg["account"],
                                  "debit": leg["debit"], "credit": leg["credit"],
                                  "batch": batch})
            return items

        entries = [e1, e2]
        clean = _mirror(entries)
        rep_clean = svc7.reconcile(clean)
        p7["clean_zero"] = rep_clean == []
        shuffled = list(reversed(clean))
        rep_shuffled = svc7.reconcile(shuffled)
        p7["shuffled_zero"] = rep_shuffled == []
        # planted: drop one leg -> missing_entry
        dropped = [i for i, it in enumerate(clean)
                   if it["account"] == "B" and it["credit"] == 100]
        missing_stream = [it for i, it in enumerate(clean) if i != dropped[0]]
        rep_missing = svc7.reconcile(missing_stream)
        p7["missing_detected"] = any(
            r.get("kind") == "missing_entry" and r.get("account") == "B"
            for r in rep_missing)
        # planted: duplicate one leg -> duplicate_posting
        dup_stream = clean + [dict(clean[0])]
        rep_dup = svc7.reconcile(dup_stream)
        p7["duplicate_detected"] = any(
            r.get("kind") == "duplicate_posting" for r in rep_dup)
        # planted: extra debit leg without its credit -> unbalanced_batch
        unbal = clean + [{"seq": 99, "account": "A", "debit": 7, "credit": 0,
                          "batch": "ghost"}]
        rep_unbal = svc7.reconcile(unbal)
        p7["unbalanced_detected"] = any(
            r.get("kind") == "unbalanced_batch" and r.get("batch") == "ghost"
            for r in rep_unbal)
        # false positives: the ghost leg is not in the ledger — must NOT be
        # reported as missing (the direction of truth is ledger -> stream)
        p7["no_reverse_missing"] = not any(
            r.get("kind") == "missing_entry" and r.get("batch") == "ghost"
            for r in rep_unbal)
    except Exception as exc:  # noqa: BLE001
        p7["error"] = repr(exc)
    out["p7"] = p7

    # -- P9: crash recovery -------------------------------------------------------------
    try:
        out["p9"] = _run_ledger_restart_probe(workspace_dir)
    except Exception as exc:  # noqa: BLE001
        out["p9"] = {"error": repr(exc)}
    return out


def _lm(idx: int, name: str, checks: list[tuple[bool, str]]):
    """One ledger milestone from (ok, label) assertions; score = passed/total."""
    from benchmark_v3.bench_harness.core.types import MilestoneResult

    passed = sum(1 for ok, _ in checks if ok)
    total = len(checks) or 1
    detail = "; ".join("%s %s" % ("PASS" if ok else "FAIL", label)
                       for ok, label in checks)
    return MilestoneResult(
        milestone_id="payment_ledger_m%d" % idx,
        name=name,
        passed=passed == total,
        score=round(passed / total, 3),
        failure_reason=None if passed == total else detail,
        diagnostics=detail,
    )


_LEDGER_MILESTONE_NAMES = (
    "复式入账与平衡校验", "转账原子性", "16 线程并发守恒", "幂等重放",
    "冲正与部分退款", "日终结算", "对账-缺失与重复", "对账-乱序与零误报",
    "崩溃恢复",
)


def build_ledger_milestones(data: dict[str, Any]) -> list[Any]:
    """Map scenario evidence to the 9 payment_ledger milestones."""
    if data.get("fatal"):
        return [_lm(i + 1, name, [(False, "fatal: %s" % str(data["fatal"])[:120])])
                for i, name in enumerate(_LEDGER_MILESTONE_NAMES)]
    p1 = data.get("p1") or {}
    p2 = data.get("p2") or {}
    p3 = data.get("p3") or {}
    p4 = data.get("p4") or {}
    p5 = data.get("p5") or {}
    p6 = data.get("p6") or {}
    p7 = data.get("p7") or {}
    p9 = data.get("p9") or {}
    rej = p1.get("rejects") or {}

    def ok(section: dict, key: str) -> bool:
        return section.get(key) is True

    ms: list[Any] = []
    ms.append(_lm(1, _LEDGER_MILESTONE_NAMES[0], [
        (ok(p1, "posted"), "平衡入账受理"),
        (ok(p1, "trial_balanced"), "试算平衡不变量"),
        (rej.get("unbalanced") is True, "不平入账拒绝"),
        (rej.get("empty") is True, "空入账拒绝"),
        (rej.get("zero_amount") is True, "零金额拒绝"),
        (rej.get("negative") is True, "负金额拒绝"),
        (rej.get("unknown_account") is True, "未知账户拒绝"),
        (rej.get("mixed_currency") is True, "跨币种混记拒绝"),
    ]))
    ms.append(_lm(2, _LEDGER_MILESTONE_NAMES[1], [
        (ok(p2, "transferred"), "转账双方入账"),
        (ok(p2, "insufficient_rejected"), "余额不足拒绝"),
        (ok(p2, "no_partial"), "拒绝无部分效果"),
        (ok(p2, "currency_mismatch_rejected"), "币种不符拒绝"),
        (ok(p2, "unknown_account_rejected"), "未知账户拒绝"),
        (ok(p2, "zero_amount_rejected"), "零金额拒绝"),
        (ok(p2, "balances_exact"), "终态余额精确"),
    ]))
    ms.append(_lm(3, _LEDGER_MILESTONE_NAMES[2], [
        (ok(p3, "all_ok"), "16 线程全部成功"),
        (ok(p3, "no_crash"), "并发无异常逃逸"),
        (ok(p3, "accounts_restored"), "移动后账户复原"),
        (ok(p3, "sink_zero"), "中转账户归零"),
        (ok(p3, "drain_exactly_10"), "16 线程抢 100 余额恰好 10 成功"),
        (ok(p3, "drain_rejected_6"), "其余 6 线程被拒"),
        (ok(p3, "drain_no_crash"), "争抢无异常"),
        (ok(p3, "drain_exact_balances"), "抢空终态精确"),
        (ok(p3, "no_negative"), "无负余额"),
        (ok(p3, "trial_balanced"), "并发后仍试算平衡"),
    ]))
    ms.append(_lm(4, _LEDGER_MILESTONE_NAMES[3], [
        (ok(p4, "post_replay_same"), "入账重放同结果"),
        (ok(p4, "post_replay_no_double"), "入账重放不双记"),
        (ok(p4, "transfer_replay_same"), "转账重放同结果"),
        (ok(p4, "transfer_replay_no_double"), "转账重放不双移"),
        (ok(p4, "trial_balanced"), "重放后仍平衡"),
    ]))
    ms.append(_lm(5, _LEDGER_MILESTONE_NAMES[4], [
        (ok(p5, "reversed_balances"), "冲正后余额还原"),
        (ok(p5, "double_reverse_rejected"), "重复冲正拒绝"),
        (ok(p5, "unknown_original_rejected"), "未知原键冲正拒绝"),
        (ok(p5, "partial_refund"), "部分退款余额正确"),
        (ok(p5, "trial_balanced"), "冲正后仍平衡"),
    ]))
    ms.append(_lm(6, _LEDGER_MILESTONE_NAMES[5], [
        (p6.get("nets_correct") is True, "净额计算正确"),
        (ok(p6, "batch_id_present"), "生成结算批次"),
        (ok(p6, "trial_balanced_after_settle"), "结算后仍平衡"),
        (ok(p6, "resettle_same_batch"), "重复结算同批次"),
        (ok(p6, "resettle_same_nets"), "重复结算同净额"),
        (p6.get("second_day_nets") is True, "次日独立结算"),
    ]))
    ms.append(_lm(7, _LEDGER_MILESTONE_NAMES[6], [
        (ok(p7, "missing_detected"), "缺失 entry 检出"),
        (ok(p7, "duplicate_detected"), "重复入账检出"),
        (ok(p7, "unbalanced_detected"), "不平批次检出"),
    ]))
    ms.append(_lm(8, _LEDGER_MILESTONE_NAMES[7], [
        (ok(p7, "clean_zero"), "干净流零报告"),
        (ok(p7, "shuffled_zero"), "乱序流零报告"),
        (ok(p7, "no_reverse_missing"), "不把流外 leg 当缺失"),
    ]))
    ms.append(_lm(9, _LEDGER_MILESTONE_NAMES[8], [
        (p9.get("A") == 760 and p9.get("B") == -1000 and p9.get("C") == 240,
         "恢复后余额精确"),
        (ok(p9, "trial_balanced"), "恢复后试算平衡"),
        (ok(p9, "replay_ok"), "恢复后幂等键仍有效"),
        (ok(p9, "settle_not_doubled"), "恢复后不重复结算"),
    ]))
    return ms


def run_ledger_scenario_isolated(workspace_dir: str | Path) -> dict[str, Any]:
    """Subprocess-isolated wrapper for run_ledger_scenario (untrusted code)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--ledger-worker",
             str(workspace_dir)],
            capture_output=True, text=True, timeout=SCENARIO_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"fatal": "ledger worker timeout after %ds"
                        % SCENARIO_TIMEOUT_SECONDS}
    if proc.returncode != 0:
        return {"fatal": "ledger worker exit %d: %s"
                % (proc.returncode, (proc.stderr or "")[-400:])}
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {"fatal": "ledger worker output unreadable: %r" % exc}
    return data if isinstance(data, dict) else {"fatal": "non-dict payload"}


if __name__ == "__main__":
    if "--ledger-worker" in sys.argv:
        _ws = sys.argv[sys.argv.index("--ledger-worker") + 1]
        print(json.dumps(run_ledger_scenario(_ws), ensure_ascii=False))
        raise SystemExit(0)
    if "--worker" in sys.argv:
        # child mode: run the scenario in-process and dump evidence as JSON
        _ws = sys.argv[sys.argv.index("--worker") + 1]
        print(json.dumps(_scenario_worker(_ws), ensure_ascii=False))
        raise SystemExit(0)
    if "--self-test" in sys.argv:
        _p, _f = self_test()
        print(f"business_task self-test: {_p} passed, {_f} failed", flush=True)
        raise SystemExit(0 if _f == 0 else 1)
