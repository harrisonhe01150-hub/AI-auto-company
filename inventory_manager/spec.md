# inventory-manager — Module spec

**Version**: 0.1.0
**Status**: Live (Lifong) — extracted 2026-05-27

Core inventory operations: parse and execute `received N units SKU` /
`sold N units SKU` / `balance SKU` / `undo` text commands. Manages a JSON
transaction ledger and computes per-SKU balances by combining catalogue
baseline with all transactions.

Out of scope (host owns):
- Yoyo close-loop `done +27... SKU N` (Lifong order workflow)
- Agent C suggestion approve/reject (admin flow)
- FAISS rebuild triggers, scheduled reports
- Permission checks (`is_manager()` etc) — host decides who can run commands

---

## Public API

```python
inv.process_command(text, recorded_by="manager") -> str | None
    # Parse and execute. Returns reply text or None if text isn't a command.

inv.apply_received(sku, qty, unit=None, recorded_by="api") -> dict
inv.apply_sold(sku, qty, unit=None, recorded_by="api")     -> dict
    # Programmatic API for non-text callers (e.g. xlsx importer).

inv.get_balance_for(sku) -> dict | None
    # {sku, name, balance, unit, out_of_stock, low_stock, status}

inv.get_all_balances() -> list[dict]
inv.undo_last() -> str
```

---

## Config schema

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `ledger_path`         | str (path) | **yes** | — | JSON file for transaction history |
| `stock_catalog_path`  | str (path) | yes¹    | — | Catalogue (Lifong nested-categories format) |
| `catalog_loader`      | callable   | yes¹    | None | Alternative: `() -> {SKU_UPPER: product_dict}` |
| `timezone`            | str        | no      | `"Africa/Johannesburg"` | For timestamps |
| `low_stock_threshold` | int        | no      | `5`     | balance <= this → triggers `on_low_stock` callback with `level="low"` |
| `on_low_stock`        | callable   | no      | noop    | `(sku, balance_info, level)` called after a `sold` if stock is low/oos |
| `on_restock`          | callable   | no      | noop    | `(sku, balance_info)` called after a `received` if balance > 0 |

¹ Provide one of `stock_catalog_path` or `catalog_loader`.

Each catalogue item must expose: `sku`, `name`, `stock_unit`, `packaging`
(format: `"N units per bale"`), and optionally `stock_quantity` (baseline).

---

## Dependencies

- Python: `pytz` (stdlib otherwise)
- Other modules: none
- External services: none

---

## Test snippet

```python
from modules.inventory_manager import init

def cat():
    return {"NC02": {"sku": "NC02", "name": "Women's Boat Socks",
                     "stock_unit": "dozen", "packaging": "420 dozen per bale",
                     "stock_quantity": 1200}}

low_calls = []
def on_low(sku, info, level): low_calls.append((sku, level))

inv = init({
    "ledger_path":         "/tmp/inv_test.json",
    "catalog_loader":      cat,
    "low_stock_threshold": 100,
    "on_low_stock":        on_low,
})

print(inv.process_command("balance NC02"))            # baseline 1200
print(inv.process_command("sold 2 bales NC02"))       # -2*420 = -840
b = inv.get_balance_for("NC02")
assert b["balance"] == 360                            # 1200 - 840
print(inv.process_command("undo"))
b2 = inv.get_balance_for("NC02")
assert b2["balance"] == 1200                          # back to baseline
```
