# 模块库 — INDEX (browse by category)

The 9 v1.0 modules grouped by what they DO. Each link goes to that module's
`spec.md` for full API + config schema + test snippet.

---

## 📡 Communication — getting messages in and out

| Module | What it does | Typical client need |
|---|---|---|
| **[whatsapp_core](./whatsapp_core/spec.md)** | Wraps Meta WhatsApp Cloud API: send text / image / document, download media, transcribe voice via Whisper. Credentials injected, no global state. | Every WhatsApp-based client |

---

## 🧠 Sales conversation — talking to customers

| Module | What it does | Typical client need |
|---|---|---|
| **[sales_brain](./sales_brain/spec.md)** | Composable Claude framework: primary + fallback model, system prompt assembly, message history, pluggable knowledge retriever / context providers / response parsers. | Any client doing AI customer service |
| **[escalation_handler](./escalation_handler/spec.md)** | Detects "speak to manager" / "talk to boss" requests and returns a configured handoff message before invoking the LLM. | Any client with a human fallback |

---

## 📦 Inventory — stock state + updates

| Module | What it does | Typical client need |
|---|---|---|
| **[inventory_manager](./inventory_manager/spec.md)** | Parses `received N units SKU` / `sold N units SKU` / `balance SKU` / `undo` text commands. Manages JSON ledger. Fires on_low_stock + on_restock callbacks. | Wholesalers, retailers tracking stock |
| **[stock_xlsx_importer](./stock_xlsx_importer/spec.md)** | Bulk inventory update from xlsx attachment. Auto-detects 5-col manifest or 2-col simple format. Best-effort with per-row error reporting. | Any client whose supplier sends xlsx manifests |
| **[restock_waitlist](./restock_waitlist/spec.md)** | Customers register interest in OOS products. When stock returns, sends multilingual WhatsApp notifications + auto-clears the list. Includes STOP opt-out. | Clients who lose sales when out of stock |

---

## 👤 Customer & product display

| Module | What it does | Typical client need |
|---|---|---|
| **[customer_memory](./customer_memory/spec.md)** | Cross-session profile per customer: name, last seen, total chats, product interests. Includes prompt-injectable context string builder. | Any client wanting personalised replies |
| **[image_sender](./image_sender/spec.md)** | Detects SKU codes in any text and dispatches matching product images via injected send callable. Hyphen-tolerant. Max-per-message cap to avoid spam. | Clients with a product photo library |

---

## 📊 Reporting

| Module | What it does | Typical client need |
|---|---|---|
| **[report_engine](./report_engine/spec.md)** | Minimal core: PDF rendering (ReportLab) + WhatsApp delivery + APScheduler cron jobs. Designed to be extended with Jinja2 templates per client. | Clients wanting daily/weekly PDF summaries |

---

## 🚧 Reserved (planned, not yet built)

These are concepts on the roadmap. They'll be added to the warehouse the
first time a paying client opts in:

| Module | Why deferred | First-client trigger |
|---|---|---|
| **payment_handler** | Lifong opted out 2026-05-27 (anti-scam concern — Yoyo manually handles all EFT confirmations). Reserved as a per-client opt-in. | First client who wants automated payment-proof OCR + Yoyo approve/reject flow (~3-4h build) |
| **upsell_logic** | No client has asked yet. | Pattern recognition: "bought X → push Y" |
| **tiktok_auto_reply** | Agent A TikTok pipeline currently hibernated. | When a client wants two-way TikTok customer flow |
| **instagram_auto_reply** | Same as above for IG DMs from customers. | When a client wants auto-reply to inbound IG enquiries |

---

## Composition recipes (common bundles)

| Client type | Recommended modules |
|---|---|
| **Small WhatsApp wholesaler** | whatsapp_core + sales_brain + escalation_handler + customer_memory + image_sender (5) |
| **Wholesaler with stock turnover** | + inventory_manager + stock_xlsx_importer + restock_waitlist (8) |
| **Wholesaler wanting daily reports** | + report_engine (9 — full Lifong setup) |
| **Retail self-serve (preset bundle for Line 2)** | whatsapp_core + sales_brain + image_sender + customer_memory (4) |
| **Chinese factory listing products** | whatsapp_core + sales_brain + image_sender (3) |

See `EXAMPLES.md` for actual Python wire-up code per recipe.

---

## Quick navigation

- **Module catalogue (this file)**: `INDEX.md`
- **Quick start**: `README.md`
- **Developer contract**: `SPEC.md`
- **Source mapping**: `INVENTORY.md`
- **Composition recipes**: `EXAMPLES.md`
- **Version history**: `CHANGELOG.md`
