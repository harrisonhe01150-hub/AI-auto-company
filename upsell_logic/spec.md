# upsell-logic — Module spec

**Version**: 0.1.0 (skeleton)
**Status**: Reserved — built but not deployed for any client yet.
No client has asked for upsell yet. Ready for the first opt-in (likely a
wholesaler who wants "you bought NC02 — these also go well: NC01, D1").

Two suggestion strategies bundled in one module:

1. **Rule-based bundles** — client declares `{SKU: [related_skus]}`. Cheap,
   predictable, easy to curate. Best for the first weeks of a deployment
   when there's no order history yet.
2. **History-based** — given the customer's past SKU enquiries/orders, find
   SKUs frequently paired with those across the order history (requires an
   `order_history_loader` injection). Falls back to bundles if no history.

---

## Public API

```python
upsell.suggest_for_sku(sku)                  -> list[dict]
    # [{"sku": str, "name": str, "reason": str}, ...]
    # Uses bundles config only

upsell.suggest_for_history(sku_history)      -> list[dict]
    # Uses bundles + statistical co-occurrence from order_history_loader

upsell.suggest(sku=None, history=None)        -> list[dict]
    # Combined ranked suggestion (single-SKU first, then history-based)

upsell.format_suggestion_message(suggestions, lang="en") -> str
    # Phone-friendly message: "🛒 You might also like:\n• *NC01* ...\n• *D1* ..."
```

---

## Config schema

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `bundles`              | dict[str, list[str]] | yes¹    | {} | `{SKU: [related_skus]}` — primary signal |
| `stock_catalog_path`   | str                  | no      | — | For looking up product names (uses Lifong nested schema) |
| `catalog_loader`       | callable             | no      | None | Alternative: `() -> {SKU: product_dict}` |
| `order_history_loader` | callable             | no      | None | `() -> [[sku, ...], ...]` for statistical suggestions |
| `is_in_stock_callable` | callable             | no      | None | `(sku) -> bool`. If set + `exclude_out_of_stock=True`, OOS SKUs are filtered |
| `exclude_out_of_stock` | bool                 | no      | True | Skip suggestions for SKUs not in stock (requires `is_in_stock_callable`) |
| `max_suggestions`      | int                  | no      | 3 | Cap on suggestions returned |
| `templates`            | dict[str, str]       | no      | builtin EN/ZH/AF/AM | `{lang: format_string}` with `{bullets}` placeholder |
| `bullet_format`        | str                  | no      | `"• *{sku}* {name}"` | Format per suggestion (`{sku}`, `{name}`, `{reason}` placeholders) |

¹ Module functions even if `bundles` is empty — but `suggest_for_sku` will
return [] until you add entries. History-based works without bundles if
`order_history_loader` is set.

---

## Dependencies

- Python: stdlib only
- Other modules: none directly
- External services: none

---

## Test snippet

```python
from modules.upsell_logic import init

def cat():
    return {
        "NC02": {"name": "Women's Boat Socks"},
        "NC01": {"name": "Men's Boat Socks"},
        "D1":   {"name": "Work Socks"},
        "HW-40": {"name": "Knitted Hat"},
        "HW-51": {"name": "Jacquard Knitted Hat"},
    }

upsell = init({
    "bundles": {
        "NC02": ["NC01", "D1"],
        "HW-40": ["HW-51"],
    },
    "catalog_loader": cat,
    "max_suggestions": 3,
})

# Single-SKU suggestion
sugs = upsell.suggest_for_sku("NC02")
assert {s["sku"] for s in sugs} == {"NC01", "D1"}

# History-based: customer has asked about NC02 and HW-40
# → bundles say NC01, D1, HW-51 are related (excluding past SKUs)
sugs = upsell.suggest_for_history(["NC02", "HW-40"])
assert len(sugs) <= 3
assert "HW-40" not in {s["sku"] for s in sugs}  # already in history

# WhatsApp message
msg = upsell.format_suggestion_message(sugs, lang="en")
print(msg)
# 🛒 You might also like:
# • *NC01* Men's Boat Socks
# • *D1* Work Socks
# • *HW-51* Jacquard Knitted Hat
```

---

## Integration pattern

After Agent B replies to a customer mentioning a SKU:

```python
# 1. Extract the SKU(s) Agent B just talked about
mentioned = imgs.find_skus_in_text(ai_reply)   # uses image_sender's detector

# 2. Get customer's history (from customer_memory module)
profile = mem.load(customer_phone)
history = profile.get("product_interests", [])

# 3. Build suggestions
sugs = upsell.suggest(
    sku=mentioned[0][0] if mentioned else None,
    history=history,
)

# 4. If we have suggestions, send a follow-up message
if sugs:
    lang = mem.load(customer_phone).get("lang", "en")
    msg  = upsell.format_suggestion_message(sugs, lang=lang)
    wa.send_text(customer_phone, msg)
```

---

## Why this is reserved / opt-in

Upsell can backfire on a relationship-driven sales channel like WhatsApp —
customers feel pressured if every reply ends with "you might also like".
Module is built BUT the host should be conservative about when to call it:
maybe only after the customer has confirmed an order, or only once per
conversation, never on first message.

Lifong (the v1.0 client) hasn't enabled this. When a future client wants
upsell, this is the foundation — they configure bundles, optionally
inject their order history, and the host wires when-to-call.
