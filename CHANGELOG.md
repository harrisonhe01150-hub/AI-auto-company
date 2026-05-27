# 模块库 — CHANGELOG

All notable changes to the warehouse are recorded here. Each module also
carries its own `__version__` string inside `module.py`.

Format: `## [warehouse-version] — YYYY-MM-DD`
within each entry, modules are grouped by `Added` / `Changed` / `Deprecated`.

---

## [v1.0] — 2026-05-27

**Initial published warehouse**. Born from the live Lifong Trading deployment
that has been running 24/7 since 2025. All 9 modules extracted from the
production codebase, refactored to take client-specific data through config
injection (JSON + env vars) so they're reusable across any wholesale / retail
WhatsApp business.

### Added — 9 modules at v0.1.0

| Module | Source in Lifong | Lines (module.py) |
|---|---|---|
| `whatsapp_core` | `src/messaging.py` (full extract + shim) | ~230 |
| `sales_brain` | `src/agent_brain.py` (composable framework; Lifong reference impl stays) | ~180 |
| `escalation_handler` | `agent_brain.py` HARD_ESCALATE block | ~80 |
| `customer_memory` | `src/customer_routing.py` profile fns | ~180 |
| `image_sender` | `webhook_router.py` `_find_all_skus_in_text` + `messaging.send_meta_image_url` | ~150 |
| `inventory_manager` | `src/stock_commands.py` (core sold/received/balance/undo) | ~240 |
| `stock_xlsx_importer` | `src/stock_xlsx_importer.py` (full extract + shim) | ~250 |
| `restock_waitlist` | `src/restock_waitlist.py` (full extract + shim) | ~210 |
| `report_engine` | New minimal core; complex Lifong PDFs stay in `agent_d_report.py` | ~150 |

### Added — 5 warehouse-level documents

- `README.md` — friendly intro + usage example + roadmap
- `INDEX.md` — modules grouped by category with composition recipes
- `INVENTORY.md` — source mapping (which Lifong code became which module)
- `SPEC.md` — module developer contract (init / config / shims)
- `EXAMPLES.md` — Python wire-up snippets for common client setups
- `CHANGELOG.md` — this file

### Extraction patterns used (3 categories)

1. **Full extract + backward-compat shim** (3 modules):
   `restock_waitlist`, `stock_xlsx_importer`, `whatsapp_core`.
   Lifong's `src/<file>.py` is now a thin shim that loads the module +
   Lifong's client config + re-exports the original function names so all
   existing callers (webhook_router / customer_routing / etc) keep working
   without edits.

2. **Standalone module** (3 modules):
   `escalation_handler`, `image_sender`, `customer_memory`.
   The logic was inline in larger files (agent_brain / webhook_router /
   customer_routing). The module lives parallel to that inline code. No
   shim needed; future refactor will replace inline calls with module calls.

3. **Framework + Lifong reference implementation stays** (3 modules):
   `inventory_manager`, `report_engine`, `sales_brain`.
   The Lifong production files (`stock_commands.py`, `agent_d_report.py`,
   `agent_brain.py`) contain so much Lifong-specific business logic that a
   clean drop-in shim wasn't safe to do in the extraction session. The
   module is the composable framework / pluggable foundation; Lifong's
   existing files stay as the "advanced reference implementation" that
   demonstrates how the module composes for the most complex case. Future
   focused sessions will migrate Lifong onto the module.

### Reserved (deferred per business decision)

- `payment_handler` — Lifong opted out 2026-05-27 (anti-scam; Yoyo manually
  handles all EFT confirmations). Build per-client when a future client
  explicitly wants payment-proof OCR + manager approve/reject (~3-4 hours).
- `upsell_logic` — No client has asked yet.
- `tiktok_auto_reply`, `instagram_auto_reply` — Agent A platform tunnels
  in development; will mature into modules when stable.

### Validation

All 9 modules passed smoke tests during extraction:

- `restock_waitlist`: dedup + 4-lang notify + opt-out
- `stock_xlsx_importer`: parsed real 工作簿3.xlsx → 58 valid + 595 expected errors
- `escalation_handler`: keyword triggers verified
- `image_sender`: 3 SKUs detected + cap respected + hyphen-tolerant
- `customer_memory`: save / load / update_from_reply + context builder
- `whatsapp_core`: shim functions exposed correctly (full integration tested via Lifong production)
- `inventory_manager`: received / sold / undo + low-stock callback
- `report_engine`: 1860-byte PDF + senders proxy
- `sales_brain`: framework loads cleanly (full integration deferred to Lifong refactor session)

---

## Upcoming (planned)

### [v1.1] — when needed

- `payment_handler` v0.1.0 (first client opt-in)
- Lifong `agent_brain.py` fully migrated onto `sales_brain` module
- `agent_d_report.py` complex PDF layouts migrated into `report_engine` as Jinja2 templates

### [v1.2] — when Agent A matures

- `tiktok_auto_reply`
- `instagram_auto_reply`

### [v2.0] — when first non-Lifong client deploys

Warehouse versioning becomes more disciplined: every module bumps its own
`__version__` for breaking changes; clients pin module versions in their
config.
