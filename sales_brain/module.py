"""
sales_brain.module — Composable LLM sales-assistant framework.

Wraps the Claude (Anthropic) call lifecycle with pluggable hooks for:
  - Early escalation (skip LLM entirely if user wants a human)
  - Knowledge retrieval (RAG, plain catalogue lookup, etc.)
  - Context provision (customer profile, watchlist, learned corrections, ...)
  - Response parsing (extract structured tokens like [YOYO_MSG], [WAITLIST_ADD])

This is the framework. Lifong's production agent_brain.py is the advanced
reference implementation (still in src/ until a focused refactor session).
Friend's project + future clients compose this module with their own hooks.

Public API (returned by init):
    handle_query(user_message, image_data=None, from_phone=None,
                 conversation_history=None, customer_context="") -> dict
        # {"status": str, "message": str, ...parser_extras}
"""
import logging
import os

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = """You are the sales assistant for {business_name}.

HOW TO TALK:
- Chat like a real person — warm, natural, like a knowledgeable friend.
- Keep messages SHORT. 2-4 lines max. WhatsApp is not email.
- Be honest: if you don't know, say so.

CUSTOMER CONTEXT:
{contexts}

PRODUCT KNOWLEDGE:
{knowledge}
"""


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class SalesBrain:
    """Configured composable sales-assistant for a single client."""

    def __init__(self, config: dict):
        if "anthropic_api_key" not in config or not config["anthropic_api_key"]:
            raise ValueError("sales_brain: missing required config: 'anthropic_api_key'")
        if "business_name" not in config:
            raise ValueError("sales_brain: missing required config: 'business_name'")

        self.api_key       = config["anthropic_api_key"]
        self.business_name = config["business_name"]

        # ── LLM routing (hybrid: DeepSeek primary + Claude fallback) ──────
        # If `deepseek_api_key` is set in config (or DEEPSEEK_API_KEY env var
        # is present), we route through LLMRouter. Otherwise fall back to
        # direct Anthropic for full backward compat.
        self.deepseek_api_key = config.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
        self.deepseek_model   = config.get("deepseek_model",   "deepseek-chat")
        self.model_primary    = config.get("model_primary",    "claude-sonnet-4-6")
        self.model_fallback   = config.get("model_fallback",   "claude-haiku-4-5-20251001")
        self.max_tokens     = int(config.get("max_tokens", 1000))
        self.system_prompt_template = config.get("system_prompt", DEFAULT_SYSTEM_PROMPT)

        # Pluggables (all optional)
        self.escalation_checker  = config.get("escalation_checker")          # (msg) -> dict|None
        self.knowledge_retriever = config.get("knowledge_retriever")         # (query, k) -> str
        self.context_providers   = list(config.get("context_providers", []))  # [(msg, phone) -> str]
        self.response_parsers    = list(config.get("response_parsers", []))   # [(text) -> (text, dict)]

        # Lazy LLM router; built on first chat call
        self._llm = None

    def _get_llm(self):
        """Return a configured LLMRouter (hybrid DeepSeek + Claude).

        If host installed src/llm_router.py (Lifong/Lolawe deployments) we use
        that. Otherwise (standalone module use) we fall back to a direct
        Anthropic client wrapper so the module still works on its own.
        """
        if self._llm is not None:
            return self._llm
        try:
            # Prefer the project-level LLMRouter when available
            from llm_router import LLMRouter
            self._llm = LLMRouter(
                deepseek_api_key=self.deepseek_api_key,
                anthropic_api_key=self.api_key,
                deepseek_model=self.deepseek_model,
                claude_primary=self.model_primary,
                claude_fallback=self.model_fallback,
            )
        except ImportError:
            # Standalone fallback: minimal inline router using only Anthropic
            from anthropic import Anthropic
            _ant = Anthropic(api_key=self.api_key)
            primary, fallback = self.model_primary, self.model_fallback
            class _AnthropicOnlyRouter:
                def chat(self, *, system_prompt, messages, max_tokens=1000, force_claude=False, require_token=None):
                    try:
                        r = _ant.messages.create(model=primary, max_tokens=max_tokens, system=system_prompt, messages=messages)
                        model_used = primary
                    except Exception:
                        r = _ant.messages.create(model=fallback, max_tokens=max_tokens, system=system_prompt, messages=messages)
                        model_used = fallback
                    text = "".join(getattr(b, "text", "") for b in r.content)
                    return {"text": text, "provider": "claude", "model": model_used,
                            "input_tokens": getattr(r.usage, "input_tokens", 0),
                            "output_tokens": getattr(r.usage, "output_tokens", 0),
                            "fallback_reason": None}
            self._llm = _AnthropicOnlyRouter()
        return self._llm

    def handle_query(self, user_message: str, image_data: str = None,
                     from_phone: str = None, conversation_history: list = None,
                     customer_context: str = "") -> dict:
        """Handle a customer query end-to-end.

        Returns a dict with at minimum {status, message}. Additional keys come
        from `response_parsers` (e.g. yoyo_message, waitlist_add_skus,
        payment_proof) — each parser merges its extracted dict in.
        """
        # ── 1. Early escalation check ──────────────────────────────────────
        if self.escalation_checker and user_message:
            try:
                hit = self.escalation_checker(user_message)
                if hit:
                    return hit
            except Exception as e:
                logger.warning(f"escalation_checker raised (continuing): {e}")

        # ── 2. Run context providers to assemble extra LLM context ─────────
        contexts = []
        if customer_context:
            contexts.append(customer_context)
        for cp in self.context_providers:
            try:
                extra = cp(user_message, from_phone)
                if extra:
                    contexts.append(extra)
            except Exception as e:
                logger.warning(f"context_provider raised (continuing): {e}")

        # ── 3. Knowledge retrieval ─────────────────────────────────────────
        knowledge = ""
        if self.knowledge_retriever:
            try:
                query = user_message if user_message else "Identify this product"
                knowledge = self.knowledge_retriever(query, 5)
                if not isinstance(knowledge, str):
                    knowledge = str(knowledge)
            except Exception as e:
                logger.warning(f"knowledge_retriever raised (continuing): {e}")

        # ── 4. Build system prompt ─────────────────────────────────────────
        system_prompt = self.system_prompt_template.format(
            business_name=self.business_name,
            contexts="\n\n".join(contexts) if contexts else "(none)",
            knowledge=knowledge or "(no product context retrieved)",
        )
        # Sanitize against surrogates that crash the API call
        system_prompt = system_prompt.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

        # ── 5. Build messages array ────────────────────────────────────────
        messages = []
        if conversation_history:
            for turn in conversation_history:
                safe = str(turn.get("content", "")).encode("utf-8", errors="replace").decode("utf-8", errors="replace")
                messages.append({"role": turn.get("role", "user"), "content": safe})

        content_blocks = []
        if image_data:
            if "," in image_data:
                header, image_b64 = image_data.split(",", 1)
                media_type = header.split(";")[0].split(":")[1] if ":" in header else "image/jpeg"
            else:
                image_b64, media_type = image_data, "image/jpeg"
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": image_b64},
            })
        if user_message:
            content_blocks.append({"type": "text", "text": user_message})
        elif image_data:
            content_blocks.append({"type": "text", "text": "Analyze this image."})
        messages.append({"role": "user", "content": content_blocks})

        # ── 6. Call LLM via hybrid router (DeepSeek → Claude fallback) ─────
        llm = self._get_llm()
        try:
            llm_response = llm.chat(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=self.max_tokens,
            )
            reply_text = llm_response["text"]
            logger.debug(
                f"sales_brain: {llm_response['provider']}/{llm_response['model']} "
                f"reply={len(reply_text)}c"
                + (f" (fallback={llm_response['fallback_reason']})" if llm_response.get("fallback_reason") else "")
            )
        except Exception as e:
            logger.error(f"sales_brain LLM call failed entirely: {e}")
            return {"status": "ERROR", "message": "Sorry, I'm having trouble right now. Please try again in a moment."}

        # ── 7. Response parsers extract structured tokens ──────────────────
        extras = {}
        for parser in self.response_parsers:
            try:
                reply_text, parsed = parser(reply_text)
                if parsed:
                    extras.update(parsed)
            except Exception as e:
                logger.warning(f"response_parser raised (continuing): {e}")

        return {"status": "SUCCESS", "message": reply_text.strip(), **extras}


def init(config: dict) -> SalesBrain:
    config = _expand_env(config)
    return SalesBrain(config)
