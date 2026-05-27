# restock-waitlist — Module spec

**Version**: 0.1.0
**Status**: Live (Lifong, since 2026-05-26 — extracted into warehouse 2026-05-27)

Lets customers register interest in out-of-stock products. When the SKU comes
back in stock (via the host system's stock-update flow), every waiting customer
receives a multilingual WhatsApp notification.

---

## Public API

```python
waitlist.add(sku: str, waid: str, name: str = "", lang: str = None) -> bool
    # True if newly added, False if existing entry refreshed

waitlist.list_for_sku(sku: str) -> list[dict]
    # All entries waiting for this SKU

waitlist.notify_for_sku(sku: str, product_name: str) -> dict
    # Sends notifications + clears waitlist for that SKU
    # Returns {"sent": int, "failed": int, "total": int}

waitlist.remove_all_for_waid(waid: str) -> int
    # Opt-out: drop every entry for this phone. Returns count removed.

waitlist.detect_language(text: str) -> str
    # Returns one of the configured languages.

waitlist.set_sender(callable) -> None
    # Late-bind the WhatsApp send function (if not provided at init time)
```

---

## Config schema

```json
{
  "business_name":   "Lifong Wholesale",
  "contact_phone":   "+27 71 773 5427",
  "storage_path":    "inventory/restock_waitlist.json",
  "languages":       ["en", "zh", "af", "am"],
  "timezone":        "Africa/Johannesburg",
  "templates":       { "en": "...", "zh": "..." },
  "af_hint_words":   ["jy", "die", "ek", "..."],
  "whatsapp_sender": "<callable injected at init time>"
}
```

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `business_name`   | str        | **yes** | — | Shown in notification messages |
| `contact_phone`   | str        | **yes** | — | Where customers should reach out (formatted string) |
| `storage_path`    | str (path) | **yes** | — | JSON file for persistent waitlist state |
| `languages`       | list[str]  | **yes** | — | Which language codes to support (subset of `en`/`zh`/`af`/`am`) |
| `timezone`        | str        | no      | `"Africa/Johannesburg"` | For `added_at` timestamps |
| `templates`       | dict       | no      | builtin fallbacks | `{lang: format_string}` with `{sku}`, `{name}`, `{business_name}`, `{contact_phone}` placeholders |
| `af_hint_words`   | list[str]  | no      | 14 common AF words | Tokens used to disambiguate Afrikaans from English (Latin-script collision) |
| `whatsapp_sender` | callable   | no      | None    | `(to: str, text: str) -> dict` — required before calling `notify_for_sku()` |

Env-var expansion: any string value starting with `$NAME` is replaced with
`os.environ["NAME"]` at init time.

---

## Dependencies

- **Python packages**: `pytz`
- **Other modules**: none directly (but `whatsapp_sender` is typically the
  `send_whatsapp_message` function from the `whatsapp-core` module)
- **External services**: WhatsApp delivery via the injected sender

---

## Persistence

The waitlist is stored as JSON at `config.storage_path`:

```json
[
  {
    "sku":      "NC02",
    "waid":     "27123456789",
    "name":     "Henry He",
    "lang":     "zh",
    "added_at": "2026-05-26T22:10:34+02:00"
  }
]
```

Concurrent calls are serialised through an instance-level `threading.Lock` so
two webhook handlers cannot corrupt the file.

---

## Integration pattern (host responsibilities)

The host system (webhook handler / scheduler) wires the module in:

1. **On customer DM about OOS product** — host detects out-of-stock product
   mention, calls `waitlist.add(sku, customer_waid, customer_name, lang)`.
2. **On stock command "received N units SKU"** — host calls
   `waitlist.notify_for_sku(sku, product_name)` after updating its ledger so
   waiting customers learn the SKU is back.
3. **On customer "STOP" message** — host calls `waitlist.remove_all_for_waid(waid)`.

The module owns the waitlist state and notification logic. The host owns
detection, stock state, and the actual WhatsApp send (via `whatsapp_sender`).

---

## Test snippet

```python
from modules.restock_waitlist import init

def fake_sender(to, text):
    print(f"[MOCK] -> {to}: {text[:80]}")
    return {"messages": [{"id": "wamid.fake"}]}

waitlist = init({
    "business_name":   "Test Co",
    "contact_phone":   "+27 11 111 1111",
    "storage_path":    "/tmp/test_waitlist.json",
    "languages":       ["en", "zh"],
    "whatsapp_sender": fake_sender,
})

assert waitlist.add("ABC123", "27123456789", "Alice", "en") is True
assert waitlist.add("ABC123", "27123456789", "Alice", "en") is False   # dedup
result = waitlist.notify_for_sku("ABC123", "Test Product")
assert result == {"sent": 1, "failed": 0, "total": 1}
assert waitlist.list_for_sku("ABC123") == []                            # cleared
```

---

## Backward-compat shim

`src/restock_waitlist.py` in the Lifong repo is now a thin shim that
constructs a Lifong-configured singleton and re-exports its methods, so
callers like `customer_routing.py` and `stock_commands.py` keep working
without edits. See `modules/SPEC.md` § "Backward compatibility shim".
