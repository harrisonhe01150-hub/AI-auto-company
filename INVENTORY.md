# Module Warehouse — Inventory

**Last updated**: 2026-05-27

This document maps every Lifong AI System module concept to its current source
code location and notes how cleanly it can be extracted into a reusable, client-
agnostic package under `modules/`.

The warehouse exists so that future clients (starting with Harrison's family-
business friend in Phase 4) can be onboarded by **picking and configuring**
existing modules rather than rebuilding from scratch.

---

## Module map (11 conceptual modules from CLAUDE.md)

| # | Module | Status | Source files | Coupling | Extraction priority |
|---|---|---|---|---|---|
| 1 | **whatsapp-core** | Live (Lifong) | `messaging.py` + `webhook_router.py` | Medium — Meta-specific webhook structure, but credentials already env-driven | High (every client needs WhatsApp) |
| 2 | **sales-brain** | Live (Lifong) | `agent_brain.py` + `build_kb.py` | **High** — FAISS path hardcoded, system prompt embeds Lifong rules, stock.json path assumed | High (Agent B is the customer-facing core) |
| 3 | **inventory-manager** | Live (Lifong) | `stock_commands.py` | **High** — Harrison/Yoyo WAIDs hardcoded, ledger path, low-stock thresholds | High |
| 4 | **report-engine** | Live (Lifong) | `agent_d_report.py` + `scheduler_jobs.py` | **High** — report layout assumes Lifong fields, recipient list hardcoded | Medium (some clients want reports, others don't) |
| 5 | **image-sender** | Live (Lifong) | parts of `webhook_router.py` (`_find_all_skus_in_text`) + `messaging.py` (`send_meta_image_url`, `get_product_image_url`) | **Low** — function-level, easy to lift | Medium (only matters if client has product photos) |
| 6 | **customer-memory** | Live (Lifong) | `customer_routing.py` (load/save profile parts) | Medium — schema generic, but path hardcoded | Medium |
| 7 | **escalation-handler** | Live (Lifong) | `agent_brain.py` `HARD_ESCALATE_KEYWORDS` block | **Low** — pure text-based, just keyword list + handoff message | Low (small but needed) |
| 8 | **restock-waitlist** | Live (Lifong, 2026-05-26) | `restock_waitlist.py` (218 lines) + hooks in `agent_brain.py`, `customer_routing.py`, `stock_commands.py`, `webhook_router.py` | Medium — main file is self-contained, but **5 cross-file hooks** need refactoring | **Highest** — newest code, used as MVP extraction validation |
| 9 | **payment-handler** | Reserved (not built) | n/a | n/a | Build per-client when needed (Lifong opted out 2026-05-26) |
| 10 | **upsell-logic** | Planned | n/a | n/a | Low (no client wants it yet) |
| 11 | **stock-xlsx-importer** | Live (Lifong, 2026-05-27) | `stock_xlsx_importer.py` (298 lines) | **Lowest** — already self-contained, only depends on `openpyxl` + `pytz` + reads `stock.json` | High (great second extraction after restock-waitlist) |

> **Note on numbering**: CLAUDE.md lists 10 modules in the warehouse table (with
> upsell-logic as #10). `stock-xlsx-importer` was added 2026-05-27 — should be
> appended to the CLAUDE.md warehouse table next time we sync that doc.

---

## Lifong-specific code that should NOT be in any module

These belong in **`clients/lifong/`** (client overlay) instead of `modules/`:

- `stock.json` (Lifong product catalog)
- `prospects.json`, `contacted.json`, `tiktok_prospects.json`, `tiktok_contacted.json`
- `restock_waitlist.json`
- `agent_b_prompt.txt` (Lifong-specific learned improvements)
- `corrections.json` (Lifong-specific Agent C corrections)
- All chat history / customer profiles
- The Lifong-specific system prompt in `agent_brain.py` (multi-language sales tone, China-Shopping-Centre context)

The eventual layout we're aiming for:

```
modules/                ← Reusable, client-agnostic
  whatsapp-core/
  sales-brain/
  inventory-manager/
  ...

clients/
  lifong/               ← Lifong's config + data overlay
    config.json
    stock.json
    prompts/agent_b.txt
    ...
  friend_business/      ← Friend's config + data overlay (Phase 4)
    config.json
    stock.json
    ...
```

A client's `config.json` declares which modules they use + that module's
configuration. The runtime composes them.

---

## Extraction order (planned)

For Lifong's Phase 4 friend project (starting after this inventory is done):

1. **restock-waitlist** ← extracted tonight as MVP (validates the spec)
2. **stock-xlsx-importer** ← second easiest, also self-contained
3. **escalation-handler** ← simple, small, useful for any client
4. **image-sender** ← function-level, useful if friend has product photos
5. **customer-memory** ← needed for any chat-based client
6. **whatsapp-core** ← needed by every client (do this before deploying friend)
7. **sales-brain** ← biggest refactor, last because it touches everything
8. **inventory-manager** ← only if friend wants Yoyo-style stock commands
9. **report-engine** ← only if friend wants daily PDF reports

Modules 9 (payment-handler) and 10 (upsell-logic) stay deferred until a client
explicitly asks.
