# 模块库 — EXAMPLES (composition recipes)

This file shows how to wire 2-9 warehouse modules together into a complete
client system. Each recipe is the actual Python you'd write in a host script
(e.g. `clients/<client>/main.py`) that imports from the warehouse + loads
the client's JSON config + injects the cross-module callable dependencies.

---

## 📋 Recipe 1 — Minimal "talking sales assistant"

**For**: A small client who just wants Claude answering WhatsApp messages
about their products. No stock tracking, no waitlist, no reports.

**Modules used**: `whatsapp_core` + `sales_brain` + `escalation_handler` (3)

```python
# clients/small_client/main.py
import json
from pathlib import Path

from modules.whatsapp_core       import init as init_wa
from modules.sales_brain         import init as init_brain
from modules.escalation_handler  import init as init_esc

CLIENT_DIR = Path(__file__).resolve().parent

def load_cfg(name):
    return {k: v for k, v in json.load(open(CLIENT_DIR / f"{name}.json")).items()
            if not k.startswith("_")}

# 1. Build the WhatsApp client
wa = init_wa(load_cfg("whatsapp_core"))

# 2. Build the escalation checker (no dependencies)
esc = init_esc(load_cfg("escalation_handler"))

# 3. Build the sales brain — pass escalation_checker as a hook
brain_cfg = load_cfg("sales_brain")
brain_cfg["escalation_checker"] = esc.check
# Optional: add a simple stock-aware knowledge function
def simple_knowledge(query, k):
    catalog = json.load(open(CLIENT_DIR / "products.json"))
    return "\n".join(f"{p['name']}: {p['description']}" for p in catalog[:k])
brain_cfg["knowledge_retriever"] = simple_knowledge
brain = init_brain(brain_cfg)

# 4. Wire to WhatsApp webhook
def handle_incoming(from_phone, text):
    result = brain.handle_query(text, from_phone=from_phone)
    wa.send_text(from_phone, result["message"])
```

---

## 📋 Recipe 2 — Wholesaler with stock tracking + photo replies

**For**: Like Recipe 1 but the client wants stock-aware replies + product
images sent when SKUs are mentioned.

**Modules used**: above + `customer_memory` + `image_sender` + `inventory_manager` (6)

```python
from modules.whatsapp_core       import init as init_wa
from modules.sales_brain         import init as init_brain
from modules.escalation_handler  import init as init_esc
from modules.customer_memory     import init as init_mem
from modules.image_sender        import init as init_imgs
from modules.inventory_manager   import init as init_inv

wa  = init_wa(load_cfg("whatsapp_core"))
esc = init_esc(load_cfg("escalation_handler"))
mem = init_mem(load_cfg("customer_memory"))
inv = init_inv(load_cfg("inventory_manager"))   # catalog + ledger

# image_sender needs the WhatsApp send callable
imgs_cfg = load_cfg("image_sender")
imgs_cfg["send_callable"] = wa.send_image_url
imgs = init_imgs(imgs_cfg)

# sales_brain composes: escalation + customer memory + stock-aware knowledge
brain_cfg = load_cfg("sales_brain")
brain_cfg["escalation_checker"] = esc.check
brain_cfg["context_providers"]  = [
    lambda msg, phone: mem.build_context_string(phone) if phone else "",
]
def stock_aware_knowledge(query, k):
    # Build a quick catalogue snippet with live balances
    balances = inv.get_all_balances()
    lines = []
    for b in balances:
        if b["sku"].lower() in query.lower():
            lines.append(f"{b['sku']} ({b['name']}): {b['status']}, {b['balance']} {b['unit']}")
    return "\n".join(lines) or "(no matching product info)"
brain_cfg["knowledge_retriever"] = stock_aware_knowledge
brain = init_brain(brain_cfg)

def handle_incoming(from_phone, text):
    # If it's a stock command from a manager (sold/received/balance/undo),
    # let inventory_manager handle it directly:
    cmd_reply = inv.process_command(text, recorded_by=from_phone)
    if cmd_reply:
        wa.send_text(from_phone, cmd_reply)
        return

    # Otherwise, run through the sales brain
    result = brain.handle_query(text, from_phone=from_phone)
    wa.send_text(from_phone, result["message"])

    # Auto-attach product images for any SKU mentioned in the reply
    imgs.send_for_text(to=from_phone, text=text + " " + result["message"])

    # Update customer memory based on the exchange
    mem.update_from_reply(from_phone, text, result["message"])
```

---

## 📋 Recipe 3 — Full Lifong-style stack

**For**: Maximum: every module, including restock waitlist + bulk xlsx
inventory updates + scheduled reports.

**Modules used**: all 9.

```python
from modules.whatsapp_core       import init as init_wa
from modules.sales_brain         import init as init_brain
from modules.escalation_handler  import init as init_esc
from modules.customer_memory     import init as init_mem
from modules.image_sender        import init as init_imgs
from modules.inventory_manager   import init as init_inv
from modules.stock_xlsx_importer import init as init_xlsx
from modules.restock_waitlist    import init as init_waitlist
from modules.report_engine       import init as init_reports

wa  = init_wa(load_cfg("whatsapp_core"))
esc = init_esc(load_cfg("escalation_handler"))
mem = init_mem(load_cfg("customer_memory"))

# Waitlist needs WhatsApp sender
wl_cfg = load_cfg("restock_waitlist")
wl_cfg["whatsapp_sender"] = wa.send_text
waitlist = init_waitlist(wl_cfg)

# Inventory manager: on_restock callback triggers waitlist notify
inv_cfg = load_cfg("inventory_manager")
inv_cfg["on_restock"]   = lambda sku, info: waitlist.notify_for_sku(sku, info["name"])
inv_cfg["on_low_stock"] = lambda sku, info, level: wa.send_text(MANAGER_WAID, f"⚠️ {sku} is {level}")
inv = init_inv(inv_cfg)

# Xlsx importer (Yoyo sends daily inventory.xlsx via WhatsApp)
xlsx = init_xlsx(load_cfg("stock_xlsx_importer"))

# Image sender
imgs_cfg = load_cfg("image_sender")
imgs_cfg["send_callable"] = wa.send_image_url
imgs = init_imgs(imgs_cfg)

# Sales brain composes everything
brain_cfg = load_cfg("sales_brain")
brain_cfg["escalation_checker"] = esc.check
brain_cfg["context_providers"]  = [lambda m, p: mem.build_context_string(p) if p else ""]
brain_cfg["knowledge_retriever"] = your_faiss_or_simple_lookup
brain_cfg["response_parsers"]    = [
    extract_yoyo_msg,      # custom function: parse [YOYO_MSG] block
    extract_waitlist_add,  # custom: parse [WAITLIST_ADD]SKU[/WAITLIST_ADD]
]
brain = init_brain(brain_cfg)

# Reports
reports_cfg = load_cfg("report_engine")
reports_cfg["whatsapp_sender"]  = wa.send_text
reports_cfg["document_sender"]  = wa.send_document
reports = init_reports(reports_cfg)

def daily_summary():
    body = "\n".join(f"{b['sku']}: {b['balance']} {b['unit']}" for b in inv.get_all_balances()[:20])
    pdf  = reports.simple_pdf_from_text("Daily Stock Summary", body)
    reports.send_pdf_to(MANAGER_WAID, pdf, "summary.pdf", "📊 EOD summary")
reports.schedule_daily("18:00", daily_summary)
reports.start_scheduler()
```

---

## 📋 Recipe 4 — Retail self-serve (preset bundle for Line 2)

**For**: A small spaza shop / clothing store wanting just WhatsApp Q&A about
their products. No stock commands, no reports, single language.

**Modules used**: `whatsapp_core` + `sales_brain` + `customer_memory` +
`image_sender` (4)

```python
# Same pattern as Recipe 2 minus inventory_manager.
# Client config defaults to single-language English, no manager escalation
# (small shop owner IS the human, customer just asks them directly via
# their normal personal WhatsApp).
```

This is the bundle that `Business 2 — Line 2 (retail self-serve)` will
auto-onboard each new retail client into. JSON config is the only file the
customer touches; everything else is provided.

---

## 🧩 Building a NEW module mid-deployment

If a client needs a behaviour the warehouse doesn't have, you build it
inside their project FIRST, then refactor it into a new warehouse module:

1. Add the logic to their `clients/<client>/main.py` (or a one-off file)
2. Ship it for the client
3. After it's proven in production, copy the logic into `modules/<new_name>/`
   following `SPEC.md` (init factory + config dict + spec.md + example config)
4. Update `INDEX.md` and `CHANGELOG.md`
5. Republish to the warehouse here

This is how the warehouse grows over time without bottlenecking on building
modules "on spec" before any client needs them.

---

## 📂 Files structure each client maintains

```
clients/<client>/
├── main.py                          ← wire-up script (~50-150 lines)
├── whatsapp_core.json               ← env-referenced credentials
├── sales_brain.json                 ← system prompt + business name + model
├── escalation_handler.json          ← keyword list + manager contact
├── customer_memory.json             ← storage path + sku regex
├── image_sender.json                ← catalogue path + max images
├── inventory_manager.json           ← ledger path + threshold
├── stock_xlsx_importer.json         ← ledger + catalog paths
├── restock_waitlist.json            ← business name + contact + templates
├── report_engine.json               ← timezone + PDF styling
├── stock.json                       ← client's product catalogue
├── prompts/agent_b.txt              ← client's full system prompt
└── data/                            ← runtime state (waitlist, profiles, ledger)
```

Total per-client config size: typically 5-15 KB of JSON. Same module code
powers them all.
