# Module Warehouse — Specification

**Version**: 0.1 (initial draft, validated by restock-waitlist MVP on 2026-05-27)

This document defines the contract every module in `modules/` must follow so
that the runtime can pick, configure, and compose them for any client.

---

## Folder layout

```
modules/
├── INVENTORY.md           ← This warehouse's catalogue (which modules exist)
├── SPEC.md                ← This file (the contract)
├── README.md              ← Quick-start for someone using the warehouse
│
└── <module_name>/         ← One folder per module, kebab-case
    ├── __init__.py        ← Exports the public init() factory
    ├── module.py          ← Main implementation (no module-level state)
    ├── spec.md            ← Module's own docs (purpose, config schema, deps)
    ├── config.example.json ← Sample client config showing every option
    └── tests/             ← Optional unit tests
        └── test_module.py
```

---

## The `init(config: dict)` contract

Every module's `__init__.py` MUST expose a callable:

```python
def init(config: dict) -> ModuleInstance:
    """Construct a configured instance of this module.

    Args:
        config: dict matching the module's spec.md config schema.
                Top-level keys are arbitrary strings.

    Returns:
        An object exposing the module's documented public API.

    Raises:
        ValueError if required config keys are missing or malformed.
    """
```

Why factory + instance (not classes-with-classmethods, not module-level functions)?

- **No global state**: two clients can use the same module with different
  configs in the same Python process without collision.
- **Testability**: tests pass a fake config dict and get an isolated instance.
- **Composition**: the runtime can hold one `RestockWaitlist` instance per
  client and route messages by client_id.

---

## Config: JSON + env vars

- **Non-sensitive config** (business name, language list, file paths, feature
  flags) lives in `clients/<client>/<module>.json`.
- **Sensitive config** (API keys, tokens, phone numbers if you want them
  rotated) lives in environment variables, referenced from JSON like
  `"contact_phone": "$CONTACT_PHONE"`.
- The `init()` factory MUST expand `$VARNAME` placeholders against `os.environ`
  before using them.

### Config schema

Every module's `spec.md` MUST include a `## Config schema` section listing:

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `business_name` | str | yes | — | Shown to customers in messages |
| `contact_phone` | str | yes | — | Where to direct enquiries |
| `storage_path` | str | no | "data/<module>.json" | Where state is persisted |
| ... | ... | ... | ... | ... |

If a required key is missing, `init()` raises `ValueError("missing required config: <key>")`.

---

## Module dependencies

Each module's `spec.md` MUST declare:

```markdown
## Dependencies

- **Python packages**: `openpyxl>=3.0`, `pytz`
- **Other modules**: `whatsapp-core` (for `send_whatsapp_message`)
- **External services**: Meta WhatsApp Cloud API (via whatsapp-core)
```

A module that depends on another module SHOULD receive that dependency
through its config (dependency injection), not import it directly:

```python
config = {
    "business_name": "Lifong Wholesale",
    "whatsapp_sender": send_whatsapp_message_callable,  # injected
}
```

This avoids circular import problems and lets tests inject mocks.

---

## Versioning

- Each module declares its version in `module.py` as `__version__ = "0.1.0"`.
- Breaking changes bump the major (`1.0.0`).
- We follow semver loosely; until 1.0, expect breaking changes on minor bumps.

When the warehouse evolves and we revise a module, older clients pinned to an
older version stay frozen — the runtime composes by reading each client's
`modules` block which lists module + version.

---

## Backward compatibility shim

When extracting an existing piece of Lifong code into `modules/<name>/`, the
**original `src/<name>.py` file MUST remain** as a thin shim:

```python
# src/restock_waitlist.py — backward-compat shim
# Real implementation moved to modules/restock_waitlist/ on 2026-05-27
from modules.restock_waitlist import init as _init

# Construct a Lifong-configured singleton so existing imports work unchanged.
import json
from pathlib import Path
_cfg_path = Path(__file__).resolve().parent.parent / "clients" / "lifong" / "restock_waitlist.json"
_instance = _init(json.loads(_cfg_path.read_text(encoding="utf-8")))

# Re-export the methods the old API exposed:
add                = _instance.add
list_for_sku       = _instance.list_for_sku
notify_for_sku     = _instance.notify_for_sku
remove_all_for_waid = _instance.remove_all_for_waid
detect_language    = _instance.detect_language
```

This keeps `customer_routing.py`, `stock_commands.py`, etc, working without
edits, and gives us time to migrate callers to the new pattern incrementally.

---

## Public API style

Each module's public methods should be:

- **Pure where possible**: same inputs → same outputs (helps tests).
- **Side-effecting methods named clearly**: `add()`, `notify_for_sku()` rather
  than `process()` (the latter hides what it does).
- **Return-rich**: return structured results (`{"sent": 3, "failed": 1}`) so
  callers can log and react, not just `None`.
- **Best-effort with try/except inside**: a module call should never crash the
  webhook handler — log errors and return an error result.

---

## Tests (optional, recommended for new modules)

Put unit tests under `modules/<name>/tests/test_module.py`. Use stub configs:

```python
def test_add_dedups():
    rw = init({"storage_path": "/tmp/test_waitlist.json",
               "business_name": "Test Co", "contact_phone": "+27 0 000 0000",
               "languages": ["en"]})
    assert rw.add("NC02", "27123456789", "Test", "en") is True
    assert rw.add("NC02", "27123456789", "Test", "en") is False  # dedup
```

Run with `python -m pytest modules/<name>/tests/`.

---

## Anti-patterns to avoid

| Bad | Why | Better |
|---|---|---|
| Module-level constants `BUSINESS_NAME = "Lifong"` | Couples module to Lifong | Read from `config["business_name"]` in `init()` |
| `from messaging import send_whatsapp_message` inside module | Circular import risk + tight coupling | Inject `config["whatsapp_sender"]` callable |
| Hardcoded paths `Path(__file__).parent.parent / "inventory"` | Breaks when reused | `config["storage_path"]` |
| `print()` for logs | Hard to silence in tests | `logging.getLogger(__name__)` |
| Global mutable state (singleton dicts) | Two clients collide | Instance attributes on the factory's returned object |
