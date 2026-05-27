# sales-brain — Module spec

**Version**: 0.1.0 (composable framework)
**Status**: Framework live — Lifong's production agent_brain.py is the
advanced reference implementation, NOT yet refactored to use this module
(left intentionally to avoid risky end-of-session rewrite of 500 lines of
production code).

This is the composable LLM sales-assistant framework. It wraps the Anthropic
Claude call lifecycle with pluggable hooks for early escalation, knowledge
retrieval, context provision, and structured response parsing.

---

## Public API

```python
brain.handle_query(user_message, image_data=None, from_phone=None,
                    conversation_history=None, customer_context="") -> dict
    # Returns:
    # {
    #   "status":  "SUCCESS" | "ESCALATED" | "ERROR",
    #   "message": "<text to send back to customer>",
    #   ...extra keys merged in by response_parsers
    # }
```

---

## Config schema

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `anthropic_api_key`   | str       | **yes** | — | Claude API key. Use `$ENV_VAR` |
| `business_name`       | str       | **yes** | — | Injected into system prompt as `{business_name}` |
| `model_primary`       | str       | no      | `"claude-sonnet-4-6"` | First-choice Claude model |
| `model_fallback`      | str       | no      | `"claude-haiku-4-5-20251001"` | Tried if primary fails |
| `max_tokens`          | int       | no      | `1000` | |
| `system_prompt`       | str       | no      | builtin minimal | Template with `{business_name}`, `{contexts}`, `{knowledge}` placeholders |
| `escalation_checker`  | callable  | no      | None | `(user_message: str) -> dict|None`. If returns dict, that's the response (LLM skipped) |
| `knowledge_retriever` | callable  | no      | None | `(query: str, k: int) -> str`. RAG / catalogue lookup output |
| `context_providers`   | list[callable] | no | [] | Each `(user_message, from_phone) -> str` adds to `{contexts}` |
| `response_parsers`    | list[callable] | no | [] | Each `(reply_text) -> (cleaned_text, extracted_dict)`. Dict keys merged into final result |

---

## Composition example (Lifong-style)

```python
from modules.sales_brain         import init as init_brain
from modules.escalation_handler  import init as init_esc
from modules.customer_memory     import init as init_mem

esc = init_esc(load_cfg("escalation_handler"))
mem = init_mem(load_cfg("customer_memory"))

def faiss_rag(query, k):
    docs = vectorstore.similarity_search(query, k=k)
    return "\n---\n".join(d.page_content for d in docs)

def customer_profile_context(user_msg, from_phone):
    return mem.build_context_string(from_phone) if from_phone else ""

def extract_yoyo_msg(text):
    extras = {}
    if "[YOYO_MSG]" in text and "[/YOYO_MSG]" in text:
        start = text.index("[YOYO_MSG]")
        extras["yoyo_message"] = text[start+len("[YOYO_MSG]"):text.index("[/YOYO_MSG]")].strip()
        text = text[:start].strip()
    return text, extras

brain = init_brain({
    "anthropic_api_key":   "$ANTHROPIC_API_KEY",
    "business_name":       "Lifong Wholesale",
    "system_prompt":       open("clients/lifong/prompts/agent_b.txt").read(),
    "escalation_checker":  esc.check,
    "knowledge_retriever": faiss_rag,
    "context_providers":   [customer_profile_context],
    "response_parsers":    [extract_yoyo_msg],
})

result = brain.handle_query("Do you have NC02?", from_phone="27...")
```

---

## Dependencies

- Python: `anthropic` (Claude SDK)
- Other modules: composes with `escalation_handler`, `customer_memory`,
  `image_sender`, `restock_waitlist` (via response_parsers + context_providers)
- External services: Anthropic Claude API

---

## Why Lifong's `agent_brain.py` is NOT yet a shim

The production file embeds 500+ lines of Lifong-specific:
- Bilingual system prompt (EN/AF/AM/ZH with sales tone rules)
- WAITLIST RULE + YOYO_MSG + STOCK DISCLOSURE business rules
- FAISS load, watchlist check, corrections load, agent_b_prompt load (4 context sources)
- 3 token extractors (YOYO_MSG, WAITLIST_ADD, web-search-leak cleanup)
- Stock-balance computation with 5-step image-mode catalog building

A clean migration is ~3-4 hours of focused work that we'll do in a dedicated
follow-up session. For tonight, the framework is here and ready for friend's
project to compose from scratch (no Lifong baggage).
