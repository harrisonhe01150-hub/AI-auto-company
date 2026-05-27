# 模块库 — Harrison He AI Automation Module Warehouse

**Version**: v1.0 · **First published**: 2026-05-27

This warehouse holds reusable, client-agnostic Python modules that compose
into complete AI customer-service systems for wholesale / retail businesses.
Born from the live Lifong Trading deployment, each module has been refactored
to take its client-specific data through a config dict rather than hardcoded
values — so a single module powers every future client.

---

## What's here (v1.0)

9 production-ready modules + 2 foundational documents:

| Document | Purpose |
|---|---|
| `INDEX.md` | Browse modules by category with use cases |
| `INVENTORY.md` | Source mapping: which Lifong code became which module |
| `SPEC.md` | The contract every module follows (init / config / shims) |
| `CHANGELOG.md` | When each module was added / updated |
| `EXAMPLES.md` | Wire-up snippets: how to assemble a client's system |

| Module | One-liner | Status |
|---|---|---|
| [`whatsapp_core`](./whatsapp_core/spec.md) | Meta WhatsApp Cloud API: text / image / document / voice | Live |
| [`sales_brain`](./sales_brain/spec.md) | Composable Claude sales-assistant framework | Live |
| [`escalation_handler`](./escalation_handler/spec.md) | Keyword-triggered handoff to human manager | Live |
| [`customer_memory`](./customer_memory/spec.md) | Cross-session customer profile storage | Live |
| [`image_sender`](./image_sender/spec.md) | SKU detection in text + batch image dispatch | Live |
| [`inventory_manager`](./inventory_manager/spec.md) | sold/received/balance/undo commands + ledger | Live |
| [`stock_xlsx_importer`](./stock_xlsx_importer/spec.md) | Bulk stock updates from xlsx attachment | Live |
| [`restock_waitlist`](./restock_waitlist/spec.md) | OOS waitlist + multilingual restock notify | Live |
| [`report_engine`](./report_engine/spec.md) | PDF + WhatsApp delivery + cron scheduling | Live |

---

## How a module is structured

Every module folder has 4 files:

```
<module_name>/
├── __init__.py              ← exports `init()` factory
├── module.py                ← implementation (no global state)
├── spec.md                  ← full docs: API, config schema, dependencies, test snippet
└── config.example.json      ← copy → client config, edit values
```

Read the module's `spec.md` first — it tells you what config it needs, what
public methods it exposes, and what it depends on.

---

## How to use a module

```python
import json
from modules.restock_waitlist import init as init_waitlist

# Load client config (one JSON file per (client, module) pair)
cfg = json.load(open("clients/lifong/restock_waitlist.json"))

# Inject any callable dependencies the module needs
from messaging import send_whatsapp_message
cfg["whatsapp_sender"] = send_whatsapp_message

# Construct a configured instance
waitlist = init_waitlist(cfg)

# Use it
waitlist.add(sku="NC02", waid="27123456789", name="Henry", lang="zh")
waitlist.notify_for_sku("NC02", "Women's Boat Socks")
```

The same module + a different JSON config powers a different client. Lifong
uses 4 languages, friend's business might use 1. Lifong's storage_path is
`inventory/restock_waitlist.json`, another client's is wherever they keep
their data. The module code doesn't change.

---

## Why a warehouse exists

> One client = a custom system. 10 clients = the same system 10 times.
> A warehouse = each new client reuses + extends instead of rebuilding.

This is the moat. The first client (Lifong, live since 2025) paid for the
modules to exist. Every future client benefits compoundingly:

- Client #1 (Lifong)        → 9 modules in warehouse
- Client #2 (friend, soon)  → reuse 6, add 1-2 new → 10-11 modules
- Client #5                 → mostly reuse, ~1 new
- Client #20                → 100% reuse, days-not-weeks deployment

Each new module added (e.g. `payment_handler` when a client wants it) becomes
inventory for every future client.

---

## Where the live Lifong code lives

The same modules also live inside the Lifong repo at:

`Documents/lifong-ai-system/modules/`

with Lifong-specific client configs at `Documents/lifong-ai-system/clients/lifong/`.

This warehouse (`OneDrive/one person company/模块库/`) is the **standalone
reference + showcase** — easy to browse, easy to share with potential clients
("here's everything I can drop into your business"). The Lifong copy is the
**production import path** that Lifong's `src/` shims actually `import` from.

Going forward, when a module is updated in the Lifong repo, it should also
be republished here (see `CHANGELOG.md` for the versioning convention).

---

## Roadmap

- [ ] Build `payment_handler` when first client opts in
- [ ] Build `upsell_logic` when a client asks
- [ ] Build `tiktok_auto_reply`, `instagram_auto_reply` as Agent A extensions mature
- [ ] Migrate Lifong's `src/agent_brain.py` to fully compose `sales_brain` (currently src/agent_brain.py is the advanced reference; module is the composable framework)
- [ ] Migrate Lifong's `src/agent_d_report.py` complex PDF layouts into `report_engine` as Jinja2 templates

---

## Owner

Harrison He — `harrisonhe.01150@gmail.com` — Johannesburg, South Africa

Built on top of the live Lifong Trading deployment as proof-of-concept for the
Harrison He AI Automation Company.
