"""
stock_xlsx_importer.module — Bulk inventory updates from an xlsx attachment.

Reads an Excel file (uploaded via WhatsApp document message or any other
ingest path), validates rows against a configurable SKU catalogue, aggregates
duplicates, and appends a batch of `received` transactions to a configurable
ledger JSON.

Supported xlsx layouts (auto-detected from header keywords):
  - 5-col (manufacturer manifest):  型号 | 货物名称 | 件数 | 每件数量 | 总数量
  - 2-col simple:                   型号/SKU | 数量/Quantity
  - Other multi-col files where the qty column is identifiable by header keyword

Catalogue access is fully injected — the module never assumes a stock.json
schema. Provide either:
  - `sku_catalog_loader`: a callable returning `{SKU_UPPER: {name, stock_unit}}`
  - or `stock_catalog_path` + leave loader unset to use the bundled
    Lifong-style nested-categories parser as a default.

Public API (returned by init):
    parse_inventory_xlsx(xlsx_bytes)          -> dict
    apply_to_ledger(valid_rows, waid, fname)  -> dict {batch_id, applied_count}
    format_whatsapp_summary(parse, apply, fn) -> str
    process_xlsx_bytes(bytes, waid, fname)    -> str   (end-to-end convenience)
"""
import json
import logging
import os
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
from pathlib import Path

import openpyxl
import pytz

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _default_lifong_catalog_loader(catalog_path: Path) -> dict:
    """Default loader for the Lifong stock.json nested-categories schema.
    Returns {SKU_UPPER: {name, stock_unit}}.

    Schema assumed:
        {
            "textiles":      [ {"sku": ..., "name": ..., "stock_unit": ...}, ... ],
            "sportswear":    [ ... ],
            "business_rules": {...}   ← skipped
        }
    """
    if not catalog_path.exists():
        return {}
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Cannot load stock catalog at {catalog_path}: {e}")
        return {}
    out = {}
    skip = {"business_rules", "categories", "escalation_policy"}
    for cat, items in data.items():
        if cat in skip or not isinstance(items, list):
            continue
        for item in items:
            sku = (item.get("sku") or "").upper()
            if sku:
                out[sku] = {
                    "name":       item.get("name", sku),
                    "stock_unit": item.get("stock_unit", "pcs"),
                }
    return out


def _coerce_int(val):
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None


class StockXlsxImporter:
    """A configured, isolated xlsx-importer instance for a single client."""

    def __init__(self, config: dict):
        for key in ("ledger_path",):
            if key not in config:
                raise ValueError(f"stock_xlsx_importer: missing required config: {key!r}")

        self.ledger_path = Path(config["ledger_path"])
        self.timezone    = pytz.timezone(config.get("timezone", "Africa/Johannesburg"))
        self.uploader_label = config.get("uploader_label", "xlsx_import")

        # Header keywords used to find the QUANTITY column. First match wins
        # in scan order. Defaults cover bilingual EN/ZH typical headers.
        self.qty_header_keywords = list(config.get("qty_header_keywords", [
            "总数量", "总数",
            "Quantity", "QUANTITY", "Qty", "QTY", "qty",
            "数量",
        ]))

        # Tokens that mark a mid-file header row (multi-batch xlsx files).
        # The default catches the manufacturer manifest pattern.
        self.mid_header_tokens = set(config.get("mid_header_tokens", {
            "型号", "SKU", "sku",
        }))

        # Catalog loader: either inject a callable or use the bundled default
        loader = config.get("sku_catalog_loader")
        if loader is None:
            catalog_path = config.get("stock_catalog_path")
            if not catalog_path:
                raise ValueError(
                    "stock_xlsx_importer: provide either `sku_catalog_loader` callable "
                    "OR `stock_catalog_path` so the module can validate SKUs"
                )
            catalog_path = Path(catalog_path)
            self._loader = lambda: _default_lifong_catalog_loader(catalog_path)
        elif callable(loader):
            self._loader = loader
        else:
            raise ValueError("sku_catalog_loader must be callable")

    # ── Parsing ────────────────────────────────────────────────────────────
    def _find_qty_column(self, headers: list):
        for kw in self.qty_header_keywords:
            for idx, h in enumerate(headers):
                if h and kw in str(h):
                    return idx
        return None

    def parse_inventory_xlsx(self, xlsx_bytes: bytes) -> dict:
        try:
            wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), data_only=True)
        except Exception as e:
            return {"format": "unknown", "qty_col": None, "valid": [], "errors": [
                {"row": 0, "sku": "", "msg": f"Cannot open xlsx: {e}"}
            ], "total_rows": 0}

        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        if not all_rows:
            return {"format": "unknown", "qty_col": None, "valid": [], "errors": [
                {"row": 0, "sku": "", "msg": "Empty workbook"}
            ], "total_rows": 0}

        headers = [str(c).strip() if c is not None else "" for c in all_rows[0]]
        qty_col = self._find_qty_column(headers)
        if qty_col is None:
            return {"format": "unknown", "qty_col": None, "valid": [], "errors": [{
                "row": 1, "sku": "",
                "msg": f"No quantity column found in headers: {headers}. "
                       f"Expected one of: {', '.join(self.qty_header_keywords)}.",
            }], "total_rows": 0}

        if qty_col >= 3:
            fmt = "5col"
        elif qty_col == 1:
            fmt = "2col"
        else:
            fmt = "other"

        catalog    = self._loader()
        aggregated = OrderedDict()
        errors     = []

        for row_idx, row in enumerate(all_rows[1:], start=2):
            if not row or all(c is None or c == "" for c in row):
                continue
            first = row[0]
            if first is None or first == "":
                continue
            first_str = str(first).strip()
            if first_str in self.mid_header_tokens:
                continue

            sku = first_str.upper()
            if qty_col >= len(row):
                errors.append({"row": row_idx, "sku": sku, "msg": "Row too short — no qty cell"})
                continue
            qty = _coerce_int(row[qty_col])
            if qty is None:
                errors.append({"row": row_idx, "sku": sku, "msg": f"Cannot parse quantity {row[qty_col]!r}"})
                continue
            if qty <= 0:
                errors.append({"row": row_idx, "sku": sku, "msg": f"Quantity must be > 0 (got {qty})"})
                continue
            if sku not in catalog:
                errors.append({"row": row_idx, "sku": sku, "msg": "SKU not in catalogue"})
                continue

            aggregated[sku] = aggregated.get(sku, 0) + qty

        valid = [{"sku": s, "qty": q} for s, q in aggregated.items()]
        return {
            "format":     fmt,
            "qty_col":    qty_col,
            "valid":      valid,
            "errors":     errors,
            "total_rows": len(all_rows) - 1,
        }

    # ── Ledger writing ─────────────────────────────────────────────────────
    def apply_to_ledger(self, valid_rows: list, uploader_waid: str, source_filename: str) -> dict:
        if not valid_rows:
            return {"batch_id": None, "applied_count": 0}

        now = datetime.now(self.timezone)
        batch_id = f"batch_xlsx_{now.strftime('%Y%m%d%H%M%S')}"
        catalog = self._loader()

        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger = {"transactions": []}
        if self.ledger_path.exists():
            try:
                with open(self.ledger_path, "r", encoding="utf-8") as f:
                    ledger = json.load(f)
                if "transactions" not in ledger:
                    ledger["transactions"] = []
            except Exception as e:
                logger.error(f"Ledger corrupt — starting fresh: {e}")
                ledger = {"transactions": []}

        applied = 0
        for r in valid_rows:
            sku  = r["sku"].upper()
            qty  = int(r["qty"])
            info = catalog.get(sku, {})
            record = {
                "id":                  f"txn_{now.strftime('%Y%m%d%H%M%S')}_{sku}_{applied:03d}",
                "type":                "received",
                "sku":                 sku,
                "product_name":        info.get("name", sku),
                "quantity_raw":        qty,
                "unit_raw":            info.get("stock_unit", "pcs"),
                "quantity_stock_unit": qty,
                "stock_unit":          info.get("stock_unit", "pcs"),
                "timestamp":           now.isoformat(),
                "date":                now.date().isoformat(),
                "recorded_by":         self.uploader_label,
                "uploader_waid":       uploader_waid or "",
                "source_file":         source_filename or "",
                "batch_id":            batch_id,
            }
            ledger["transactions"].append(record)
            applied += 1

        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, ensure_ascii=False)

        logger.info(f"xlsx import: {applied} entries written, batch_id={batch_id}")
        return {"batch_id": batch_id, "applied_count": applied}

    # ── Formatting for WhatsApp ────────────────────────────────────────────
    def format_whatsapp_summary(self, parse_result: dict, apply_result: dict, filename: str) -> str:
        valid  = parse_result.get("valid", [])
        errors = parse_result.get("errors", [])
        fmt    = parse_result.get("format", "unknown")

        if not valid and not errors:
            return (
                f"😕 I read {filename} but found no rows to import.\n"
                f"Format detected: {fmt}.\n"
                f"Expected: a header with quantity column "
                f"({', '.join(self.qty_header_keywords[:4])}...)."
            )

        catalog = self._loader()
        lines = []
        if valid:
            lines.append(f"✅ Updated *{len(valid)} SKU(s)* from {filename}")
            lines.append(f"📋 Format: {fmt} | Batch ID: {apply_result.get('batch_id', 'n/a')}")
            lines.append("")
            lines.append("📦 Received:")
            for r in valid[:15]:
                unit  = catalog.get(r["sku"], {}).get("stock_unit", "pcs")
                name  = catalog.get(r["sku"], {}).get("name", "")
                label = f" ({name})" if name and name != r["sku"] else ""
                lines.append(f"  • {r['sku']}{label}: +{r['qty']:,} {unit}")
            if len(valid) > 15:
                lines.append(f"  ... and {len(valid) - 15} more SKUs")
        else:
            lines.append(f"⚠️ No valid rows in {filename}")

        if errors:
            lines.append("")
            lines.append(f"⚠️ *{len(errors)} error(s)* (these rows were skipped):")
            for e in errors[:10]:
                lines.append(f"  • Row {e['row']} {e['sku']}: {e['msg']}")
            if len(errors) > 10:
                lines.append(f"  ... and {len(errors) - 10} more errors")
            lines.append("")
            lines.append("Fix these rows + resend the file, or ignore (only valid rows above were applied).")

        return "\n".join(lines)

    # ── Convenience end-to-end ─────────────────────────────────────────────
    def process_xlsx_bytes(self, xlsx_bytes: bytes, uploader_waid: str, filename: str = "") -> str:
        parse_result = self.parse_inventory_xlsx(xlsx_bytes)
        apply_result = self.apply_to_ledger(parse_result["valid"], uploader_waid, filename)
        return self.format_whatsapp_summary(parse_result, apply_result, filename or "your file")


# ── Public factory ─────────────────────────────────────────────────────────
def init(config: dict) -> StockXlsxImporter:
    """Construct a configured StockXlsxImporter instance.

    See ``modules/stock_xlsx_importer/spec.md`` for the full config schema.
    """
    config = _expand_env(config)
    return StockXlsxImporter(config)
