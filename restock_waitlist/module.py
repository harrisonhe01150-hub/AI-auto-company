"""
restock_waitlist.module — Restock notification waitlist (client-agnostic).

Lets customers register interest in out-of-stock products. When the SKU comes
back in stock (via the host system's stock-update flow), every waiting customer
gets a notification via the injected `whatsapp_sender` callable.

All client-specific data (business name, contact phone, supported languages,
notification templates, storage path) is supplied through `init(config)`.

This is the client-agnostic, configurable rewrite of `src/restock_waitlist.py`
(2026-05-26 monolithic) extracted into the module warehouse on 2026-05-27.

Public API (returned by init):
    add(sku, waid, name="", lang=None)        -> bool   (True if newly added)
    list_for_sku(sku)                          -> list
    notify_for_sku(sku, product_name)          -> dict   {sent, failed, total}
    remove_all_for_waid(waid)                  -> int    (count removed)
    detect_language(text)                       -> str   one of config languages
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Lock

import pytz

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


# ── Built-in defaults usable by any client ────────────────────────────────
DEFAULT_LANGUAGES = ["en"]

# Templates can be FULLY overridden via config["templates"]; this is just a
# safe fallback if a client does not supply one for a language they configured.
FALLBACK_TEMPLATES = {
    "en": (
        "🎉 Good news! *{sku}* ({name}) is back in stock at {business_name}.\n"
        "Contact us *{contact_phone}* to order.\n"
        "(Reply STOP to opt out.)"
    ),
    "zh": (
        "🎉 好消息！*{sku}*({name})已重新到货 — {business_name}。\n"
        "联系我们 *{contact_phone}* 下单。\n"
        "(回复 STOP 取消订阅)"
    ),
    "af": (
        "🎉 Goeie nuus! *{sku}* ({name}) is weer in voorraad by {business_name}.\n"
        "Kontak ons *{contact_phone}* om te bestel.\n"
        "(Antwoord STOP om af te meld.)"
    ),
    "am": (
        "🎉 መልካም ዜና! *{sku}* ({name}) በ{business_name} እንደገና ይገኛል።\n"
        "ለማዘዝ *{contact_phone}* ያግኙን።\n"
        "(ለማቆም STOP ይላኩ።)"
    ),
    "pt": (
        "🎉 Boa notícia! *{sku}* ({name}) está de volta ao estoque em {business_name}.\n"
        "Entre em contato *{contact_phone}* para encomendar.\n"
        "(Responda STOP para cancelar.)"
    ),
}


def _expand_env(value):
    """Expand $VARNAME placeholders in a config value against os.environ."""
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class RestockWaitlist:
    """A configured, isolated restock waitlist for a single client."""

    def __init__(self, config: dict):
        # Required config
        for key in ("business_name", "contact_phone", "storage_path", "languages"):
            if key not in config:
                raise ValueError(f"restock_waitlist: missing required config: {key!r}")

        self.business_name = config["business_name"]
        self.contact_phone = config["contact_phone"]
        self.storage_path  = Path(config["storage_path"])
        self.languages     = list(config["languages"]) or DEFAULT_LANGUAGES

        # Optional: per-client template overrides (merge over fallback)
        client_templates = config.get("templates", {}) or {}
        self.templates = {**FALLBACK_TEMPLATES, **client_templates}

        # Optional: timezone for `added_at` timestamps (default SA)
        tz_name = config.get("timezone", "Africa/Johannesburg")
        self.tz = pytz.timezone(tz_name)

        # Optional: language-detect Afrikaans hint words (configurable)
        self.af_hint_words = set(config.get("af_hint_words", [
            "jy", "die", "is", "het", "kan", "nie", "asseblief",
            "dankie", "hallo", "goeie", "ek", "ons", "wat", "vir",
        ]))

        # Injected dependency: function (to: str, text: str) -> dict
        # Must return a dict; an "error" key indicates failure.
        self._whatsapp_sender = config.get("whatsapp_sender")
        if self._whatsapp_sender is None:
            logger.warning(
                "restock_waitlist init without whatsapp_sender — "
                "notify_for_sku() will be a no-op until set_sender() is called"
            )

        self._lock = Lock()

    def set_sender(self, sender_callable):
        """Late-bind the WhatsApp sender callable (useful for lazy init)."""
        self._whatsapp_sender = sender_callable

    # ── Storage I/O ────────────────────────────────────────────────────────
    def _load(self) -> list:
        if not self.storage_path.exists():
            return []
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"restock_waitlist load failed: {e}")
            return []

    def _save(self, entries: list) -> None:
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"restock_waitlist save failed: {e}")

    # ── Language detection (script-based, lightweight) ────────────────────
    def detect_language(self, text: str) -> str:
        """Detect language by Unicode script. Falls back to first configured language."""
        default = self.languages[0] if self.languages else "en"
        if not text:
            return default
        if "am" in self.languages and any("ሀ" <= c <= "፿" for c in text):
            return "am"
        if "zh" in self.languages and any("一" <= c <= "鿿" for c in text):
            return "zh"
        if "af" in self.languages:
            tokens = set(text.lower().split())
            if self.af_hint_words & tokens:
                return "af"
        if "en" in self.languages:
            return "en"
        return default

    # ── Public API ─────────────────────────────────────────────────────────
    def add(self, sku: str, waid: str, name: str = "", lang: str = None) -> bool:
        """Add (or refresh) a waitlist entry. Dedups on (sku, waid)."""
        sku  = (sku  or "").strip().upper()
        waid = (waid or "").strip().lstrip("+")
        if not sku or not waid:
            logger.warning(f"add() skipped: empty sku={sku!r} or waid={waid!r}")
            return False
        if not lang or lang not in self.templates:
            lang = self.languages[0] if self.languages else "en"

        with self._lock:
            entries = self._load()
            for e in entries:
                if e.get("sku", "").upper() == sku and e.get("waid", "").lstrip("+") == waid:
                    e["added_at"] = datetime.now(self.tz).isoformat()
                    e["lang"]     = lang or e.get("lang", "en")
                    if name:
                        e["name"] = name
                    self._save(entries)
                    logger.info(f"Waitlist refresh: sku={sku} waid={waid}")
                    return False
            entries.append({
                "sku":      sku,
                "waid":     waid,
                "name":     name or "",
                "lang":     lang,
                "added_at": datetime.now(self.tz).isoformat(),
            })
            self._save(entries)
            logger.info(f"Waitlist add: sku={sku} waid={waid} lang={lang}")
            return True

    def list_for_sku(self, sku: str) -> list:
        sku = (sku or "").strip().upper()
        return [e for e in self._load() if e.get("sku", "").upper() == sku]

    def notify_for_sku(self, sku: str, product_name: str) -> dict:
        """Send notifications to everyone on the waitlist for `sku`, then clear."""
        sku = (sku or "").strip().upper()
        if not sku:
            return {"sent": 0, "failed": 0, "total": 0}
        if self._whatsapp_sender is None:
            logger.error("notify_for_sku skipped: no whatsapp_sender injected")
            return {"sent": 0, "failed": 0, "total": 0}

        with self._lock:
            entries = self._load()
            targets = [e for e in entries if e.get("sku", "").upper() == sku]
            if not targets:
                logger.info(f"notify_for_sku({sku}): nobody waiting")
                return {"sent": 0, "failed": 0, "total": 0}
            remaining = [e for e in entries if e.get("sku", "").upper() != sku]
            self._save(remaining)

        sent = failed = 0
        for e in targets:
            lang = e.get("lang") if e.get("lang") in self.templates else self.languages[0]
            tpl = self.templates.get(lang, FALLBACK_TEMPLATES["en"])
            msg = tpl.format(
                sku=sku, name=product_name or sku,
                business_name=self.business_name, contact_phone=self.contact_phone,
            )
            try:
                result = self._whatsapp_sender(e["waid"], msg)
                if isinstance(result, dict) and "error" in result:
                    failed += 1
                    logger.warning(f"Notify failed: waid={e['waid']} sku={sku} err={result.get('error')}")
                else:
                    sent += 1
                    logger.info(f"Notified waid={e['waid']} for restocked {sku}")
            except Exception as exc:
                failed += 1
                logger.error(f"Notify exception: waid={e['waid']} sku={sku} exc={exc}")

        logger.info(f"notify_for_sku({sku}): sent={sent} failed={failed} total={len(targets)}")
        return {"sent": sent, "failed": failed, "total": len(targets)}

    def remove_all_for_waid(self, waid: str) -> int:
        """Opt-out: drop every entry for the given customer phone."""
        waid = (waid or "").strip().lstrip("+")
        if not waid:
            return 0
        with self._lock:
            entries   = self._load()
            before    = len(entries)
            remaining = [e for e in entries if e.get("waid", "").lstrip("+") != waid]
            self._save(remaining)
        removed = before - len(remaining)
        if removed:
            logger.info(f"Opt-out: removed {removed} waitlist entries for waid={waid}")
        return removed


# ── Public factory ─────────────────────────────────────────────────────────
def init(config: dict) -> RestockWaitlist:
    """Construct a configured RestockWaitlist instance.

    See ``modules/restock_waitlist/spec.md`` for the full config schema.
    """
    config = _expand_env(config)
    return RestockWaitlist(config)
