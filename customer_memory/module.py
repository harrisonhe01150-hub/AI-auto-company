"""
customer_memory.module — Cross-session customer profile storage.

Persists a dict per customer (keyed by phone) with whatever fields the host
wants to remember: name, last seen, products they've asked about, total chats,
language, etc. Hosts can also opt into the bundled `update_from_reply()`
heuristic that extracts product SKUs from bot replies + customer name from
a "[YOYO_MSG] 客户姓名：..." block.

Originally lived in customer_routing.py as load/save/update functions.
Extracted into the warehouse on 2026-05-27.

Public API:
    load(phone) -> dict
    save(phone, updates: dict)            # merges into existing
    update_from_reply(phone, user_msg, bot_reply, yoyo_message=None)
    build_context_string(phone)            # for injection into LLM prompt
    get_recent_history(phone, n=6)         # last n exchanges as LLM messages (v0.2)
    all_phones() -> list[str]              # for admin / debugging
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from threading import Lock

import pytz

__version__ = "0.2.0"

logger = logging.getLogger(__name__)


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class CustomerMemory:
    """A configured customer-profile store for a single client."""

    def __init__(self, config: dict):
        if "storage_path" not in config:
            raise ValueError("customer_memory: missing required config: 'storage_path'")

        self.storage_path = Path(config["storage_path"])
        self.tz = pytz.timezone(config.get("timezone", "Africa/Johannesburg"))

        # SKU regex used by update_from_reply to extract product interests
        # from bot replies. Default catches Lifong-style SKUs (A1, NC02, HW-40 etc).
        self.sku_pattern = re.compile(
            config.get("sku_regex", r"\b([A-Z]{1,4}[-#]?\d{1,4}[-\w]*)\b")
        )

        # Max product interests to remember per customer (LRU-ish — keep last N)
        self.max_interests = int(config.get("max_interests", 10))

        # Max conversation exchanges (user+bot pairs) to keep per customer (v0.2)
        self.max_history = int(config.get("max_history", 12))

        # Name-extract token used in handoff messages (Lifong: 客户姓名：)
        self.name_extract_token = config.get("name_extract_token", "客户姓名：")
        self.max_name_length    = int(config.get("max_name_length", 50))

        # Customer context template — injected into LLM prompts.
        # Supported placeholders: {bullets} (auto-built list)
        self.context_template = config.get(
            "context_template",
            "CUSTOMER PROFILE (use this silently — do not reveal you stored it):\n{bullets}"
        )

        self._lock = Lock()

    # ── Storage I/O ────────────────────────────────────────────────────────
    def _load_all(self) -> dict:
        if not self.storage_path.exists():
            return {}
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"customer_memory load failed: {e}")
            return {}

    def _save_all(self, data: dict):
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"customer_memory save failed: {e}")

    # ── Public API ─────────────────────────────────────────────────────────
    def load(self, phone: str) -> dict:
        """Return the profile dict for `phone`, or {} if none."""
        return self._load_all().get(phone, {})

    def save(self, phone: str, updates: dict):
        """Merge `updates` into the profile for `phone`."""
        with self._lock:
            profiles = self._load_all()
            profile  = profiles.get(phone, {})
            profile.update(updates)
            profiles[phone] = profile
            self._save_all(profiles)

    def update_from_reply(self, phone: str, user_message: str, bot_reply: str,
                          yoyo_message: str = None):
        """Extract useful info from a completed exchange and update the profile."""
        try:
            profile = self.load(phone)
            today = datetime.now(self.tz).strftime("%Y-%m-%d")
            profile["last_seen_date"] = today
            profile["total_chats"]    = profile.get("total_chats", 0) + 1

            # Extract customer name from the host's handoff message if present
            if yoyo_message and self.name_extract_token in yoyo_message:
                try:
                    for line in yoyo_message.split("\n"):
                        if self.name_extract_token in line:
                            extracted = line.split(self.name_extract_token, 1)[1].strip()
                            if extracted and "[" not in extracted and len(extracted) < self.max_name_length:
                                profile["name"] = extracted
                            break
                except Exception:
                    pass

            # Extract product SKUs mentioned in the bot reply
            mentioned = list(set(self.sku_pattern.findall((bot_reply or "").upper())))
            if mentioned:
                existing = profile.get("product_interests", [])
                combined = list(dict.fromkeys(existing + mentioned))  # dedupe, preserve order
                profile["product_interests"] = combined[-self.max_interests:]

            # v0.2: record the exchange so the bot has multi-turn context next message
            history = profile.get("history", [])
            history.append({
                "u":  (user_message or "")[:1000],
                "b":  (bot_reply or "")[:1000],
                "ts": datetime.now(self.tz).isoformat(),
            })
            profile["history"] = history[-self.max_history:]

            self.save(phone, profile)
        except Exception as e:
            logger.error(f"customer_memory update_from_reply failed for {phone}: {e}")

    def get_recent_history(self, phone: str, n: int = 6) -> list:
        """Return the last `n` exchanges as an LLM-ready messages list:
        [{"role": "user", ...}, {"role": "assistant", ...}, ...]. Oldest first."""
        try:
            history = self.load(phone).get("history", [])[-n:]
            msgs = []
            for h in history:
                if h.get("u"):
                    msgs.append({"role": "user", "content": h["u"]})
                if h.get("b"):
                    msgs.append({"role": "assistant", "content": h["b"]})
            return msgs
        except Exception as e:
            logger.warning(f"customer_memory get_recent_history failed for {phone}: {e}")
            return []

    def build_context_string(self, phone: str) -> str:
        """Build a prompt-injectable context string from the profile.
        Returns "" if no useful data."""
        profile = self.load(phone)
        if not profile:
            return ""

        today = datetime.now(self.tz).strftime("%Y-%m-%d")
        name      = profile.get("name", "")
        interests = profile.get("product_interests", [])
        last_seen = profile.get("last_seen_date", "")
        is_returning_day = last_seen and last_seen != today

        parts = []
        if name:
            parts.append(f"Customer name: {name}")
        if interests:
            parts.append(f"Previously enquired about: {', '.join(interests[-5:])}")
        if is_returning_day and name:
            parts.append(f"Returning customer — last spoke on {last_seen}. Greet them warmly by name.")
        elif is_returning_day:
            parts.append(f"Returning customer — last spoke on {last_seen}.")

        if not parts:
            return ""
        bullets = "\n".join(f"- {p}" for p in parts)
        return self.context_template.format(bullets=bullets)

    def all_phones(self) -> list:
        """Return list of all known customer phones (for admin / debugging)."""
        return list(self._load_all().keys())


def init(config: dict) -> CustomerMemory:
    config = _expand_env(config)
    return CustomerMemory(config)
