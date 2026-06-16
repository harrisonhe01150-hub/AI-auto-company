"""
escalation_handler.module — Detect customer requests to escalate to a human
and return a configured handoff response.

Pure-text module: matches a configurable list of keywords against the user's
message; if any match, returns a structured response containing the manager's
contact info. The host (Agent B) returns this response unchanged and skips
calling the LLM.

Originally lived as a HARD_ESCALATE_KEYWORDS block in agent_brain.py
(2026-05-13). Extracted into the warehouse on 2026-05-27.
"""
import logging
import os
import re

__version__ = "0.1.1"

logger = logging.getLogger(__name__)

DEFAULT_KEYWORDS = [
    # English
    "speak to manager", "speak to boss",
    "talk to manager", "talk to boss",
    "contact manager", "contact boss",
    "call manager", "call boss",
    "want manager", "want boss",
    "need manager", "need boss",
]

DEFAULT_MESSAGE_TEMPLATE = (
    "Sure! Please contact our manager {manager_name} directly:\n"
    "*WhatsApp: {manager_contact}* 😊"
)


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class EscalationHandler:
    """A configured keyword-based escalation detector."""

    def __init__(self, config: dict):
        for key in ("manager_name", "manager_contact"):
            if key not in config:
                raise ValueError(f"escalation_handler: missing required config: {key!r}")

        self.manager_name    = config["manager_name"]
        self.manager_contact = config["manager_contact"]
        # Keep keywords lowercase for case-insensitive matching.
        kws = config.get("keywords", DEFAULT_KEYWORDS)
        self.keywords = [k.lower() for k in kws]
        # Word-boundary patterns for ASCII keywords ("sale" must NOT match
        # "wholesale"); non-ASCII keywords (e.g. Amharic) keep substring match.
        self._patterns = [
            re.compile(r"\b" + re.escape(k) + r"\b") if k.isascii() else None
            for k in self.keywords
        ]
        self.message_template = config.get("message_template", DEFAULT_MESSAGE_TEMPLATE)
        self.status_code = config.get("status_code", "ESCALATED")

    def check(self, user_message: str):
        """Return {"status": ..., "message": ...} if the message triggers escalation,
        else None. Callers should short-circuit and return this directly."""
        if not user_message:
            return None
        msg_lower = user_message.lower()
        if any(
            (pat.search(msg_lower) if pat else (kw in msg_lower))
            for kw, pat in zip(self.keywords, self._patterns)
        ):
            return {
                "status":  self.status_code,
                "message": self.message_template.format(
                    manager_name=self.manager_name,
                    manager_contact=self.manager_contact,
                ),
            }
        return None


def init(config: dict) -> EscalationHandler:
    """Construct a configured EscalationHandler instance.
    See ``modules/escalation_handler/spec.md`` for the full config schema."""
    config = _expand_env(config)
    return EscalationHandler(config)
