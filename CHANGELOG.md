# 模块库 — CHANGELOG

All notable changes to the warehouse are recorded here. Each module also
carries its own `__version__` string inside `module.py`.

Format: `## [warehouse-version] — YYYY-MM-DD`
within each entry, modules are grouped by `Added` / `Changed` / `Deprecated` / `Fixed`.

---

## [v1.2] — 2026-06-07

### 🚨 Fixed — OneDrive sync truncation (3 modules)

A drift audit on 2026-06-07 revealed that three module.py files in the
warehouse copy had been silently truncated by OneDrive's mid-write sync at
v1.1 release time, leaving them unusable for client onboarding:

| Module | Truncated at | Lost lines |
|---|---|---|
| `payment_handler/module.py` | mid-line `r` (line 325) | 20 lines (rest of `reject()` + `get_pending()` + `list_pending()` + `init()`) |
| `restock_waitlist/module.py` | `def init(config: dict) -> Resto` (line 256) | 7 lines (`init()` factory) |
| `upsell_logic/module.py` | `conf` (line 226) | 2 lines (`init()` factory) |

Without `init()` factories, the modules were unusable — clients picking them
out of the warehouse couldn't instantiate.

Files restored from the Lifong main-repo source of truth (which was never
truncated — only the OneDrive-synced copy suffered).

This validates Dead Order #3 ("OneDrive sync may produce conflicting versions
— one truncated, one with duplicated content"). Files now restored via single
atomic Write per Dead Order #3 guidance.

A future `scripts/verify_warehouse.py` (Task #69) will run `ast.parse` on
every module.py + a pre-commit hook to prevent silent truncation from
shipping again.

### Changed — sales_brain hybrid LLM (graduate from Lifong)

`sales_brain` now supports `deepseek_api_key` + `deepseek_model` config
fields. When `deepseek_api_key` (or env var `DEEPSEEK_API_KEY`) is present,
the module routes calls through `LLMRouter` (DeepSeek primary, Claude
fallback) — ~13× cost reduction with no quality drop on typical sales
queries. Falls back gracefully to direct Anthropic when `LLMRouter` is not
on the host's Python path (standalone module use).

This change graduates the hybrid LLM optimisation that's been validated in
Lifong production since 2026-06-04. Backward compatible — existing
config.json files without `deepseek_api_key` keep working with Claude only.

Module-internal version `__version__` stays at `0.1.0` because the public
API is unchanged — only the LLM routing implementation evolved.

---

## [v1.1] — 2026-05-27 (same-day follow-up)

### Added — 2 reserved-but-built modules at v0.1.0

Both modules are skeletons — no live client deployed yet but the code is
production-ready for the first opt-in.

| Module | What it does |
|---|---|
| `payment_handler` | EFT payment-proof workflow: vision extract → dedup check → PAY-XXX ID → manager approve/reject command → multilingual customer confirmation |
| `upsell_logic` | Rule-based bundles + history-based co-occurrence + combined ranked suggestion + multilingual format |

Both follow the established patterns from v1.0 (init factory + JSON config +
env-var expansion + callable injection for dependencies + smoke-test verified).

⚠️ **Note (added retroactively in v1.2):** these v1.1 published files were
later discovered to be truncated by OneDrive sync. Fixed in v1.2.

### Updated

- `INDEX.md`: payment_handler moved under new 💰 Money category;
  upsell_logic moved under new 🛒 Cross-sell category; reserved list shrunk
  to just the two "concept only" entries (tiktok_auto_reply, instagram_auto_reply).

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

### [v1.3] — Lifong Graph API migration (Task #67)

When Lifong's Meta-verified Business unlocks Instagram Graph API access (in
review now), the following modules will be added to the warehouse:

- `instagram_publisher_official` — Meta Graph API content publish (preferred)
- `instagram_publisher_playwright` — abstracted from Lifong's `agent_a_ad_engine.py` (fallback for clients without Meta verification)
- `instagram_dm_autoreply` — Instagram Messaging API (within 24h customer reply window)
- `instagram_comment_autoreply` — Graph API comment hook
- `instagram_insights` — weekly performance report via Graph API

### [v1.4] — Automated graduation (Task #69)

`scripts/graduate_modules.py` will run weekly (Windows Task Scheduler /
Railway cron) to:

1. Diff `lifong-ai-system/modules/` against the warehouse copy
2. Run `ast.parse` on every module.py to detect truncation
3. Auto-copy diffs, bump warehouse `__version__`, write CHANGELOG entry
4. Git commit + push warehouse repo
5. Report via Harrison's WhatsApp

This prevents silent drift (like v1.1 → v1.2 took 10 days to discover).

### [v2.0] — when first non-Lifong client deploys

Warehouse versioning becomes more disciplined: every module bumps its own
`__version__` for breaking changes; clients pin module versions in their
config.
