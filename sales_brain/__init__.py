"""
sales_brain module — public init() factory.

Composable LLM sales-assistant framework. Provides the Claude-call scaffolding
(primary + fallback model, system prompt assembly, message history, response
parsing) and lets the client wire in their own:

  - knowledge_retriever: (query, k) -> str   (Lifong: FAISS RAG; others: simpler lookup)
  - context_providers:   list of (user_message, from_phone) -> str
  - response_parsers:    list of (reply_text) -> (cleaned_text, extracted_dict)
  - escalation_checker:  (user_message) -> dict|None  (early-return before LLM)

Usage:
    from modules.sales_brain import init as init_brain

    brain = init_brain({
        "anthropic_api_key":  "$ANTHROPIC_API_KEY",
        "business_name":      "Your Business",
        "system_prompt":      "You are the sales assistant for {business_name}...",
        "knowledge_retriever": faiss_lookup,
        "context_providers":   [load_customer_profile_string],
        "response_parsers":    [extract_yoyo_msg, extract_waitlist_add],
        "escalation_checker":  escalation_handler.check,
    })

    result = brain.handle_query("Do you have NC02?", from_phone="27...")
    # -> {"status": "SUCCESS", "message": "...", "yoyo_message": None, "waitlist_add_skus": []}

For Lifong's current production behaviour see src/agent_brain.py — that file
is intentionally NOT yet refactored to use this module (too large/coupled to
do cleanly without a focused session). This module is the framework that
NEW clients (e.g. the family-business friend in Phase 4) will compose.
"""
from .module import init, SalesBrain, __version__

__all__ = ["init", "SalesBrain", "__version__"]
