# escalation-handler — Module spec

**Version**: 0.1.0
**Status**: Live (Lifong) — extracted 2026-05-27

Detects when a customer explicitly asks to speak to a human (manager/boss/owner)
and short-circuits the LLM with a configured handoff message containing the
manager's WhatsApp contact.

---

## Public API

```python
esc.check(user_message: str) -> dict | None
    # If any keyword matches → {"status": "ESCALATED", "message": "..."}
    # Else → None
```

---

## Config schema

```json
{
  "manager_name":     "Yoyo",
  "manager_contact":  "+27 84 579 1010",
  "keywords":         ["speak to manager", "talk to boss", ...],
  "message_template": "Sure! Please contact {manager_name}: *WhatsApp: {manager_contact}* 😊",
  "status_code":      "ESCALATED"
}
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `manager_name`     | str        | **yes** | — | Inserted into message template |
| `manager_contact`  | str        | **yes** | — | Phone/WhatsApp number string |
| `keywords`         | list[str]  | no      | 12 EN defaults | Phrases matched case-insensitively; substring match |
| `message_template` | str        | no      | builtin EN | Format string with `{manager_name}` and `{manager_contact}` placeholders |
| `status_code`      | str        | no      | `"ESCALATED"` | Returned in the `status` field |

Env-var expansion: any string value starting with `$NAME` is replaced.

---

## Dependencies

- Python: none beyond stdlib
- Other modules: none
- External services: none

---

## Integration pattern

Host (Agent B) calls `esc.check()` BEFORE invoking the LLM:

```python
hit = esc.check(user_message)
if hit:
    return hit   # Skip LLM, send handoff message directly
# else proceed with LLM
```

---

## Test snippet

```python
from modules.escalation_handler import init

esc = init({"manager_name": "Yoyo", "manager_contact": "+27 84 579 1010"})

assert esc.check("Hi, what's the price for NC02?") is None
assert esc.check("can I speak to manager?")["status"] == "ESCALATED"
assert "Yoyo" in esc.check("contact your boss")["message"]
```
