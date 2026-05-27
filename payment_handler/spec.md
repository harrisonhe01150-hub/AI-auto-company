# payment-handler — Module spec

**Version**: 0.1.0 (skeleton)
**Status**: Reserved — built but not deployed for any client yet.
Lifong opted out 2026-05-27 (anti-scam: Yoyo handles all EFT confirmations
manually). Ready for the first opt-in client.

End-to-end payment-proof workflow: customer sends bank screenshot via WhatsApp →
vision callable extracts amount/reference/bank/sender/date → entry saved with
sequential PAY-XXX ID + multilingual ack to customer + manager notified with
approve/reject command syntax → on `approve <PAY-XXX>`, customer gets
multilingual confirmation; on `reject <PAY-XXX> [reason]`, customer gets a
polite decline + manager contact.

---

## Public API

```python
pay.submit_proof(image_b64, customer_waid, customer_name="", lang="en") -> dict
    # End-to-end. Returns:
    # {payment_id, is_duplicate, extracted, customer_reply_sent, manager_notified}

pay.approve(payment_id, manager_waid)                       -> dict
pay.reject(payment_id, manager_waid, reason="")             -> dict

pay.get_pending(payment_id)                                  -> dict | None
pay.list_pending(status=None)                                -> list
pay.is_duplicate_reference(reference, bank=None)             -> bool
```

---

## Config schema

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `storage_path`    | str (path) | **yes** | — | JSON file for pending+approved+rejected payments |
| `business_name`   | str        | **yes** | — | Used in customer messages |
| `manager_name`    | str        | **yes** | — | Used in customer messages + manager notification |
| `manager_waid`    | str        | **yes** | — | WhatsApp ID that receives notifications + can approve/reject |
| `manager_phone`   | str        | no      | manager_waid | Display string in customer messages (e.g. "+27 84 579 1010") |
| `languages`       | list[str]  | no      | `["en"]` | Customer reply language pool |
| `timezone`        | str        | no      | `"Africa/Johannesburg"` | For timestamps |
| `vision_callable` | callable   | **yes for use** | None | `(image_b64, prompt) -> {amount, reference, bank, sender, date, recipient}` |
| `vision_prompt`   | str        | no      | builtin | Override the prompt sent to your vision model |
| `whatsapp_sender` | callable   | **yes for use** | None | `(to, text) -> dict` for ack / notification / approval / rejection |
| `templates`       | dict       | no      | builtin EN/ZH/AF/AM | `{stage: {lang: format_string}}` — override per stage per language |
| `duplicate_check` | bool       | no      | `True`  | Reject second submission of same reference |
| `id_prefix`       | str        | no      | `"PAY"` | PAY-001, PAY-002, ... |

Env-var expansion: any string value starting with `$NAME` is replaced at init.

---

## Notification templates (4 stages × 4 languages)

Built-in stages: `submitted`, `approved`, `rejected`, `duplicate`.

Placeholders supported per stage:
- `{business_name}`, `{manager_name}`, `{manager_phone}`
- `{reference}`, `{amount}` (approved + duplicate)
- `{reason}` (rejected)

---

## Dependencies

- Python: `pytz`
- Other modules: none (composes with `whatsapp_core` for the sender, and
  Claude / GPT vision for the extractor — both injected as callables)
- External services: WhatsApp (for sends), Claude vision / GPT-4V / etc. (for image extract)

---

## Persistence shape

```json
[
  {
    "id":            "PAY-001",
    "customer_waid": "27123456789",
    "customer_name": "Henry He",
    "lang":          "zh",
    "amount":        15000,
    "reference":     "ABC123",
    "bank":          "FNB",
    "sender":        "Henry He",
    "date_on_proof": "2026-05-27",
    "recipient":     "Lifong Wholesale",
    "submitted_at":  "2026-05-27T15:30:00+02:00",
    "status":        "pending",
    "actioned_by":   null,
    "actioned_at":   null,
    "reject_reason": null
  }
]
```

---

## Integration pattern (host responsibilities)

```python
# 1. On WhatsApp image attachment from a customer:
result = pay.submit_proof(image_b64, customer_waid, customer_name, lang)
# (module handles vision + dedup + save + customer ack + manager notify)

# 2. On manager text command:
match = re.match(r"^(approve|reject)\s+(PAY-\d+)(?:\s+(.+))?$", msg, re.IGNORECASE)
if match:
    verb, pid, reason = match.group(1).lower(), match.group(2).upper(), match.group(3) or ""
    if verb == "approve":
        result = pay.approve(pid, manager_waid=sender)
    else:
        result = pay.reject(pid, manager_waid=sender, reason=reason)
    send_to_manager(f"{result.get('ok') and '✅' or '❌'} {pid}: {result}")
```

---

## Test snippet

```python
from modules.payment_handler import init

def fake_vision(b64, prompt):
    return {"amount": 1500, "reference": "TEST123", "bank": "FNB",
            "sender": "Alice", "date": "2026-05-27", "recipient": "Test Co"}

sent = []
def fake_send(to, text):
    sent.append((to, text[:60]))
    return {"messages": [{"id": "fake"}]}

pay = init({
    "storage_path":    "/tmp/pay_test.json",
    "business_name":   "Test Co",
    "manager_name":    "Boss",
    "manager_waid":    "27000",
    "manager_phone":   "+27 00 000 0000",
    "languages":       ["en", "zh"],
    "vision_callable": fake_vision,
    "whatsapp_sender": fake_send,
})

# Submit
r = pay.submit_proof("fake_b64", "27123", "Alice", "en")
assert r["payment_id"] == "PAY-001"
assert not r["is_duplicate"]
assert len(sent) == 2   # ack to customer + notify to manager

# Resubmit same — should detect duplicate
r2 = pay.submit_proof("fake_b64", "27123", "Alice", "en")
assert r2["is_duplicate"] is True

# Approve
r3 = pay.approve("PAY-001", manager_waid="27000")
assert r3["ok"] is True
```

---

## Why this is reserved / opt-in

EFT payment screenshots are TRIVIALLY forged (any phone can edit a screenshot
in 30 seconds). A vision model can read what's on the image but cannot verify
the transaction actually cleared in the bank. The module's role is to
**triage + structure** the data so the manager can decide quickly — it is
NOT a fraud-prevention substitute for the manager's eyes on the actual bank
account.

Lifong's decision (2026-05-27) was: Yoyo just checks the bank app herself and
manually confirms with the customer. For clients with higher trust thresholds
or higher volumes, this module gives the workflow without removing the
manager's final gate.
