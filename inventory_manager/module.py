"""
inventory_manager.module — Core inventory operations: sold/received/balance/undo.

Parses text commands like "received 5 bales NC02" or "balance HW-40", appends
to a JSON ledger, and computes balances by combining the catalogue baseline
with all transactions. Fires injectable callbacks on low-stock / restock
events so the host can wire up notifications (WhatsApp alerts, restock
waitlist triggers, etc.).

Originally lived in src/stock_commands.py. Extracted into the warehouse on
2026-05-27 as the inventory-manager module.

Out of scope (kept in the host's Lifong-specific shim):
  - Yoyo "done +27... SKU N" close-loop command (Lifong order workflow)
  - Agent C "approve N" / "reject N" suggestion handling (admin flow)
  - Triggering FAISS rebuild (build_kb.py is Lifong-specific)
  - Cron-based scheduled reports (the report-engine module owns those)

Public API (returned by init):
    process_command(text, recorded_by="manager")    -> str | None
        # Parse and execute "received N units SKU", "sold N units SKU",
        # "balance SKU", "stock SKU", or "undo". Returns the reply text,
        # or None if `text` is not a recognised command.

    apply_received(sku, qty, unit=None, recorded_by="api") -> dict
    apply_sold(sku, qty, unit=None, recorded_by="api")     -> dict
        # Programmatic API for non-text callers (e.g. the xlsx importer).

    get_balance_for(sku)         -> dict | None  ({sku, name, balance, unit, status})
    get_all_balances()           -> list[dict]
    undo_last()                  -> str
"""
import inspect
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from threading import Lock

import pytz

__version__ = "0.1.1"

logger = logging.getLogger(__name__)


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


_STOCK_CMD_RE = re.compile(
    r"^(sold|received)\s+(\d+(?:\.\d+)?)\s*"
    r"(bales?|dozens?|pcs?|pieces?|pairs?|sets?|units?)?\s+"
    r"([A-Za-z0-9#\-]+)\s*$",
    re.IGNORECASE,
)
_BALANCE_CMD_RE = re.compile(r"^(?:balance|stock)\s+([A-Za-z0-9#\-]+)\s*$", re.IGNORECASE)
_UNDO_CMD_RE    = re.compile(r"^undo\s*$", re.IGNORECASE)


def _default_lifong_catalog_loader(catalog_path: Path) -> dict:
    """Default loader for Lifong nested stock.json. Returns {SKU_UPPER: product_dict}."""
    if not catalog_path.exists():
        return {}
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"inventory_manager catalog load failed: {e}")
        return {}
    out = {}
    skip = {"business_rules", "categories", "escalation_policy"}
    for cat, items in data.items():
        if cat in skip or not isinstance(items, list):
            continue
        for item in items:
            sku = (item.get("sku") or "").upper()
            if sku:
                out[sku] = item
    return out


class InventoryManager:
    """Configured inventory operations for a single client."""

    def __init__(self, config: dict):
        if "ledger_path" not in config:
            raise ValueError("inventory_manager: missing required config: 'ledger_path'")

        self.ledger_path = Path(config["ledger_path"])
        self.tz = pytz.timezone(config.get("timezone", "Africa/Johannesburg"))
        self.low_stock_threshold = int(config.get("low_stock_threshold", 5))

        loader = config.get("catalog_loader")
        if loader is None:
            catalog_path = config.get("stock_catalog_path")
            if not catalog_path:
                raise ValueError(
                    "inventory_manager: provide either `catalog_loader` OR `stock_catalog_path`"
                )
            catalog_path = Path(catalog_path)
            self._loader = lambda: _default_lifong_catalog_loader(catalog_path)
        elif callable(loader):
            self._loader = loader
        else:
            raise ValueError("catalog_loader must be callable")

        # Callbacks (fire-and-forget — host wires their own logic)
        # Guard (v0.1.1): fail FAST at boot if the host wired a callback with the
        # wrong arity — previously this crashed silently inside a thread at runtime.
        for cb_name, expected in (("on_low_stock", 3), ("on_restock", 2)):
            cb = config.get(cb_name)
            if cb is not None and callable(cb):
                try:
                    n = len(inspect.signature(cb).parameters)
                except (TypeError, ValueError):
                    continue
                if n != expected:
                    raise ValueError(
                        f"inventory_manager: callback {cb_name!r} must accept exactly "
                        f"{expected} args (got {n}) — fix the host wiring. "
                        f"Signatures: on_low_stock(sku, info, level), on_restock(sku, info)"
                    )
        self.on_low_stock = config.get("on_low_stock") or (lambda sku, info, level: None)
        self.on_restock   = config.get("on_restock")   or (lambda sku, info:        None)

        self._lock = Lock()

    # ── Helpers ────────────────────────────────────────────────────────────
    def _bale_multiplier(self, product: dict) -> int:
        packaging = product.get("packaging", "")
        m = re.search(r"(\d[\d,]*)\s+\w+\s+per\s+bale", packaging, re.IGNORECASE)
        return int(m.group(1).replace(",", "")) if m else 1

    def _convert_to_stock_unit(self, quantity: float, unit_raw: str, product: dict) -> float:
        stock_unit = product.get("stock_unit", "pcs").lower()
        u = (unit_raw or "").lower().strip()
        if u in ("bale", "bales"):
            return quantity * self._bale_multiplier(product)
        if u in ("dozen", "dozens"):
            return quantity * 12 if stock_unit in ("pcs", "pairs", "pieces", "units") else quantity
        return quantity

    def _load_ledger(self) -> dict:
        if not self.ledger_path.exists():
            return {"transactions": []}
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "transactions" not in data:
                data["transactions"] = []
            return data
        except Exception as e:
            logger.error(f"Ledger corrupt — starting fresh: {e}")
            return {"transactions": []}

    def _save_ledger(self, ledger: dict):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, ensure_ascii=False)

    def _append_record(self, record: dict):
        with self._lock:
            ledger = self._load_ledger()
            ledger["transactions"].append(record)
            self._save_ledger(ledger)

    # ── Programmatic apply ─────────────────────────────────────────────────
    def _apply(self, kind: str, sku: str, qty: float, unit: str, recorded_by: str) -> dict:
        catalog = self._loader()
        sku_u = (sku or "").upper()
        product = catalog.get(sku_u)
        if not product:
            return {"error": f"SKU '{sku}' not found"}
        if not unit:
            unit = product.get("stock_unit", "pcs")
        qty_su = self._convert_to_stock_unit(float(qty), unit, product)

        now = datetime.now(self.tz)
        record = {
            "id":                  f"txn_{now.strftime('%Y%m%d%H%M%S')}_{sku_u}",
            "type":                kind,
            "sku":                 sku_u,
            "product_name":        product.get("name", sku_u),
            "quantity_raw":        int(qty),
            "unit_raw":            unit,
            "quantity_stock_unit": int(qty_su),
            "stock_unit":          product.get("stock_unit", "pcs"),
            "timestamp":           now.isoformat(),
            "date":                now.date().isoformat(),
            "recorded_by":         recorded_by,
        }
        self._append_record(record)

        # Fire callbacks (best-effort in background)
        try:
            balance = self.get_balance_for(sku_u)
            if kind == "sold" and balance:
                level = "out_of_stock" if balance["balance"] <= 0 else (
                    "low" if balance["balance"] <= self.low_stock_threshold else None
                )
                if level:
                    threading.Thread(
                        target=self.on_low_stock,
                        args=(sku_u, balance, level),
                        daemon=True,
                    ).start()
            elif kind == "received" and balance and balance["balance"] > 0:
                # Could be a restock crossing OOS->in-stock; let host decide
                threading.Thread(
                    target=self.on_restock,
                    args=(sku_u, balance),
                    daemon=True,
                ).start()
        except Exception as e:
            logger.warning(f"Inventory callback fired with error (non-fatal): {e}")

        return {"ok": True, "record": record}

    def apply_received(self, sku: str, qty: float, unit: str = None, recorded_by: str = "api") -> dict:
        return self._apply("received", sku, qty, unit, recorded_by)

    def apply_sold(self, sku: str, qty: float, unit: str = None, recorded_by: str = "api") -> dict:
        return self._apply("sold", sku, qty, unit, recorded_by)

    # ── Balance queries ────────────────────────────────────────────────────
    def get_balance_for(self, sku: str) -> dict:
        sku_u = (sku or "").upper()
        catalog = self._loader()
        product = catalog.get(sku_u)
        if not product:
            return None
        balance = product.get("stock_quantity", 0) or 0
        ledger = self._load_ledger()
        for txn in ledger.get("transactions", []):
            if txn.get("sku", "").upper() != sku_u:
                continue
            qty = txn.get("quantity_stock_unit", 0) or 0
            if txn.get("type") == "sold":
                balance -= qty
            elif txn.get("type") == "received":
                balance += qty
        balance = max(0, balance)
        return {
            "sku":          sku_u,
            "name":         product.get("name", sku_u),
            "balance":      balance,
            "unit":         product.get("stock_unit", "pcs"),
            "out_of_stock": balance <= 0,
            "low_stock":    0 < balance <= self.low_stock_threshold,
            "status":       "out_of_stock" if balance <= 0 else "available",
        }

    def get_all_balances(self) -> list:
        return [b for b in (self.get_balance_for(sku) for sku in self._loader().keys()) if b]

    # ── Undo ────────────────────────────────────────────────────────────────
    def undo_last(self) -> str:
        with self._lock:
            ledger = self._load_ledger()
            txns = ledger.get("transactions", [])
            if not txns:
                return "Nothing to undo — no transactions recorded yet."
            last = txns.pop()
            ledger["transactions"] = txns
            self._save_ledger(ledger)
        verb = "Sold" if last.get("type") == "sold" else "Received"
        ts   = last.get("timestamp", "")[:16].replace("T", " ")
        return (
            "Undone!\n"
            f"Removed: {verb} {last.get('quantity_raw')} {last.get('unit_raw')} "
            f"{last.get('sku')} - {last.get('product_name', '')}\n"
            f"Recorded at: {ts}"
        )

    # ── Text command processor ─────────────────────────────────────────────
    def process_command(self, text: str, recorded_by: str = "manager") -> str:
        """Return reply text, or None if `text` is not a recognised command."""
        text = (text or "").strip()
        if not text:
            return None

        if _UNDO_CMD_RE.match(text):
            return self.undo_last()

        bm = _BALANCE_CMD_RE.match(text)
        if bm:
            sku = bm.group(1).upper()
            b = self.get_balance_for(sku)
            if not b:
                return f"SKU '{sku}' not found."
            return (
                f"Stock: {b['sku']} - {b['name']}\n"
                f"Balance: {b['balance']} {b['unit']}\n"
                f"Status: {'Out of Stock' if b['out_of_stock'] else ('Low Stock - reorder soon' if b['low_stock'] else 'Available')}"
            )

        m = _STOCK_CMD_RE.match(text)
        if not m:
            return None
        kind     = m.group(1).lower()
        qty_raw  = float(m.group(2))
        unit_raw = (m.group(3) or "").lower().strip()
        sku_raw  = m.group(4).upper()
        result   = self._apply(kind, sku_raw, qty_raw, unit_raw or None, recorded_by)
        if "error" in result:
            return "Error: " + result["error"]
        rec = result["record"]
        verb = "Sold" if kind == "sold" else "Received"
        return (
            f"{verb} recorded!\n"
            f"SKU: {rec['sku']} - {rec['product_name']}\n"
            f"Qty: {rec['quantity_raw']} {rec['unit_raw']} "
            f"({rec['quantity_stock_unit']} {rec['stock_unit']})\n"
            f"Time: {rec['timestamp'][11:16]} SA"
        )


def init(config: dict) -> InventoryManager:
    config = _expand_env(config)
    return InventoryManager(config)
