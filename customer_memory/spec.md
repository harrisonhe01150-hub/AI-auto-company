# customer-memory — Module spec

**Version**: 0.1.0
**Status**: Live (Lifong) — extracted 2026-05-27

Cross-session customer profile storage. Persists a dict per customer
(keyed by phone number) with arbitrary fields. Bundles a heuristic helper
(`update_from_reply`) that auto-extracts SKU mentions and customer names.

---

## Public API

```python
mem.load(phone) -> dict
mem.save(phone, updates)                            # merges into existing
mem.update_from_reply(phone, user_msg, bot_reply,
                       yoyo_message=None)            # heuristic auto-extract
mem.build_context_string(phone) -> str              # for LLM prompt injection
mem.all_phones() -> list[str]
```

---

## Config schema

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `storage_path`        | str  | **yes** | — | JSON file for the {phone: profile} map |
| `timezone`            | str  | no | `"Africa/Johannesburg"` | For `last_seen_date` field |
| `sku_regex`           | str  | no | Lifong default | Regex used by `update_from_reply` to find SKUs in bot replies |
| `max_interests`       | int  | no | `10` | Cap on product_interests list per customer |
| `name_extract_token`  | str  | no | `"客户姓名："` | Token in handoff messages where the customer name follows |
| `max_name_length`     | int  | no | `50` | Sanity cap on extracted name |
| `context_template`    | str  | no | builtin | Template for `build_context_string` with `{bullets}` placeholder |

---

## Persisted profile shape (example)

```json
{
  "27123456789": {
    "name":              "Henry He",
    "last_seen_date":    "2026-05-27",
    "total_chats":       4,
    "product_interests": ["NC02", "HW-40", "D1"]
  }
}
```

The host can freely add other fields via `save()` (preferred language, last
order date, etc) — the module doesn't enforce a schema beyond what its own
helpers write.

---

## Dependencies

- Python: stdlib + `pytz`
- Other modules: none
- External services: none

---

## Test snippet

```python
from modules.customer_memory import init

mem = init({"storage_path": "/tmp/cm.json"})
mem.save("27000", {"name": "Test"})
assert mem.load("27000")["name"] == "Test"

mem.update_from_reply("27000",
    user_message="hi",
    bot_reply="Sure! NC02 is R25/dozen and HW-40 is R8.50/piece.",
    yoyo_message=None)
profile = mem.load("27000")
assert "NC02" in profile["product_interests"]
assert "HW-40" in profile["product_interests"]
assert profile["total_chats"] == 1
```
