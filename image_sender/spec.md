# image-sender — Module spec

**Version**: 0.1.0
**Status**: Live (Lifong) — extracted 2026-05-27

Detects SKU codes in a piece of text (usually a customer message or an Agent B
reply), looks up each one's product image URL from a catalogue, and dispatches
the images via an injected `send_callable`. Sends are queued in background
threads so the caller (webhook handler) doesn't block.

---

## Public API

```python
imgs.find_skus_in_text(text: str) -> list[(sku, url)]
    # In-text detection. Word-boundary + hyphen-tolerant.

imgs.send_for_skus(to, skus, max_images=None, caption="") -> dict
    # Dispatch images. {sent, failed, total, queued}.

imgs.send_for_text(to, text, max_images=None) -> dict
    # Convenience: detect + send in one call.
```

---

## Config schema

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `send_callable`       | callable | **yes** | — | `(to: str, image_url: str, caption: str) -> dict` |
| `stock_catalog_path`  | str      | yes¹    | — | Path to Lifong-style stock.json (when no loader injected) |
| `skus_loader`         | callable | yes¹    | None | Alternative: `() -> list[(sku_upper, url)]` |
| `image_url_field`     | str      | no      | `"image_url"` | Field name in catalog item |
| `fallback_field`      | str      | no      | `"image"` | Fallback field if primary missing |
| `max_images_per_msg`  | int      | no      | `4` | Cap on images per outgoing reply (avoid spam) |

¹ Provide one of `stock_catalog_path` or `skus_loader`.

---

## Dependencies

- Python: `re`, `threading` (stdlib only)
- Other modules: none
- External services: whatever `send_callable` talks to (typically WhatsApp via
  the host's `send_meta_image_url`).

---

## Integration pattern

After Agent B replies, the host calls:

```python
all_skus_mentioned = (
    imgs.find_skus_in_text(customer_message) +
    imgs.find_skus_in_text(ai_reply)
)
# Dedup while preserving order
seen = set(); deduped = []
for s, u in all_skus_mentioned:
    if s not in seen:
        deduped.append((s, u)); seen.add(s)
imgs.send_for_skus(to=customer_phone, skus=deduped)
```

---

## Test snippet

```python
from modules.image_sender import init

def fake_send(to, url, caption):
    print(f"[MOCK] {to} <- {url}")
    return {"messages": [{"id": "wamid.fake"}]}

def fake_catalog():
    return [
        ("NC02", "https://example.com/nc02.jpg"),
        ("D1",   "https://example.com/d1.jpg"),
        ("HW-40", "https://example.com/hw40.jpg"),
    ]

imgs = init({"send_callable": fake_send, "skus_loader": fake_catalog, "max_images_per_msg": 2})

found = imgs.find_skus_in_text("Got it for NC02 and D1!")
assert {s for s, _ in found} == {"NC02", "D1"}
imgs.send_for_skus(to="27000000000", skus=found)
```
