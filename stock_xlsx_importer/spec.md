# stock-xlsx-importer — Module spec

**Version**: 0.1.0
**Status**: Live (Lifong, since 2026-05-27 — extracted into warehouse same day)

Parses an inventory xlsx (5-col manufacturer manifest OR 2-col simple
SKU+qty layout) and appends a batch of `received` transactions to a JSON
ledger. Auto-detects format from header keywords. Catalogue lookup is
fully injectable so the module isn't tied to any specific stock schema.

---

## Public API

```python
importer.parse_inventory_xlsx(xlsx_bytes: bytes) -> dict
    # {"format": "5col"|"2col"|"other"|"unknown",
    #  "qty_col": int|None,
    #  "valid":  [{"sku": str, "qty": int}, ...],
    #  "errors": [{"row": int, "sku": str, "msg": str}, ...],
    #  "total_rows": int}

importer.apply_to_ledger(valid_rows, uploader_waid: str, source_filename: str) -> dict
    # {"batch_id": str|None, "applied_count": int}

importer.format_whatsapp_summary(parse_result, apply_result, filename) -> str
    # Phone-friendly reply summarising the import

importer.process_xlsx_bytes(xlsx_bytes, uploader_waid: str, filename: str) -> str
    # End-to-end convenience: parse + apply + format
```

---

## Config schema

```json
{
  "ledger_path":          "runtime_data/stock_ledger.json",
  "stock_catalog_path":   "inventory/stock.json",
  "sku_catalog_loader":   null,
  "timezone":             "Africa/Johannesburg",
  "uploader_label":       "yoyo_xlsx",
  "qty_header_keywords":  ["总数量", "数量", "Quantity", "Qty"],
  "mid_header_tokens":    ["型号", "SKU"]
}
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `ledger_path`         | str (path) | **yes** | — | Where to append `received` transactions |
| `stock_catalog_path`  | str (path) | yes¹  | — | Path to Lifong-style stock.json (used when no loader injected) |
| `sku_catalog_loader`  | callable   | yes¹  | None | Alternative: `() -> {SKU_UPPER: {name, stock_unit}}`. Use when your catalogue isn't a Lifong-shaped JSON file |
| `timezone`            | str        | no      | `"Africa/Johannesburg"` | For ledger timestamps |
| `uploader_label`      | str        | no      | `"xlsx_import"` | Recorded as `recorded_by` in each ledger entry |
| `qty_header_keywords` | list[str]  | no      | EN+ZH defaults | Header substrings searched (in order) to locate the QUANTITY column |
| `mid_header_tokens`   | list[str]  | no      | `["型号", "SKU", "sku"]` | First-column values that mark a mid-file header row to skip (handles multi-batch xlsx files) |

¹ Provide **one of** `stock_catalog_path` (with Lifong nested-categories
schema) OR `sku_catalog_loader` (any custom loader). If both, the loader wins.

Env-var expansion: any string value starting with `$NAME` is replaced with
`os.environ["NAME"]` at init time.

---

## Dependencies

- **Python packages**: `openpyxl>=3.0`, `pytz`
- **Other modules**: none
- **External services**: none (pure parser + file I/O)

---

## Ledger entry format

Each row applied generates:

```json
{
  "id":                  "txn_20260527160000_NC02_000",
  "type":                "received",
  "sku":                 "NC02",
  "product_name":        "Women's Boat Socks",
  "quantity_raw":        2100,
  "unit_raw":            "dozen",
  "quantity_stock_unit": 2100,
  "stock_unit":          "dozen",
  "timestamp":           "2026-05-27T16:00:00+02:00",
  "date":                "2026-05-27",
  "recorded_by":         "yoyo_xlsx",
  "uploader_waid":       "27845791010",
  "source_file":         "inventory_2026-05-27.xlsx",
  "batch_id":            "batch_xlsx_20260527160000"
}
```

All rows from one `apply_to_ledger()` call share the same `batch_id` — this
enables a future "undo entire batch" feature.

---

## Integration pattern (host responsibilities)

The host system (webhook handler) wires the module in:

1. **On document attachment** — host detects `msg_type == "document"` and an
   `.xlsx` filename, downloads the bytes from the messaging service.
2. **Permission check** — host decides who's allowed to upload (Lifong:
   Yoyo + Harrison only).
3. **Process & reply** — host calls
   `importer.process_xlsx_bytes(bytes, waid, filename)` and sends the
   returned string back to the uploader.

The module owns parsing + validation + ledger writing + reply formatting.
The host owns transport, permissions, and the actual message send.

---

## Test snippet

```python
from modules.stock_xlsx_importer import init

def fake_catalog():
    return {
        "NC02": {"name": "Women's Boat Socks", "stock_unit": "dozen"},
        "D1":   {"name": "Work Socks",          "stock_unit": "dozen"},
    }

importer = init({
    "ledger_path":        "/tmp/test_ledger.json",
    "sku_catalog_loader": fake_catalog,
    "timezone":           "Africa/Johannesburg",
})

# A minimal 2-col xlsx (built in-memory):
import openpyxl
from io import BytesIO
wb = openpyxl.Workbook()
ws = wb.active
ws.append(["SKU", "Quantity"])
ws.append(["NC02", 1500])
ws.append(["D1",   200])
ws.append(["BAD",  10])      # invalid SKU -> error row
buf = BytesIO()
wb.save(buf); buf.seek(0)

result = importer.parse_inventory_xlsx(buf.read())
assert result["format"] == "2col"
assert len(result["valid"]) == 2          # NC02 + D1
assert len(result["errors"]) == 1         # BAD
assert result["valid"][0] == {"sku": "NC02", "qty": 1500}
```

---

## Backward-compat shim

`src/stock_xlsx_importer.py` in the Lifong repo is now a thin shim that
constructs a Lifong-configured singleton and re-exports `process_xlsx_bytes`
so `webhook_router.py` keeps working without edits.
