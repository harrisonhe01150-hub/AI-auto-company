"""
upsell_logic.module — Related-product suggestions.

Two suggestion strategies in one module:

  1. Rule-based "bundles" — client declares which SKUs are commonly bought
     together: `{"NC02": ["NC01", "D1"], ...}`. Cheap, predictable, easy to
     curate. Best for the first few weeks of a deployment when there's no
     order history to learn from.

  2. History-based — given a list of SKUs a customer has previously enquired
     about / ordered, find SKUs that other customers commonly pair with
     those. Requires `order_history_loader` injection — a callable returning
     `[[sku, sku, ...], ...]` (one list per past order). Falls back gracefully
     to bundles if no history is available.

  3. (Optional later) LLM-suggested — pass a `suggester_callable` that takes
     a context dict and returns suggested SKUs. Not implemented in v0.1.

Reserved 2026-05-27 — no client has asked for upsell yet. Built as a skeleton
ready for the first opt-in client (likely a wholesale client wanting "you
bought NC02 — these usually go together: NC01, D1").

Public API (returned by init):
    suggest_for_sku(sku)                                 -> list[dict]
    suggest_for_history(sku_history)                     -> list[dict]
    suggest(sku=None, history=None)                       -> list[dict]  (combined)
    format_suggestion_message(suggestions, lang="en")    -> str
"""
import json
import logging
import os
from collections import Counter
from pathlib import Path

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


DEFAULT_TEMPLATES = {
    "en": "🛒 You might also like:\n{bullets}",
    "zh": "🛒 您可能也喜欢：\n{bullets}",
    "af": "🛒 Jy mag ook hou van:\n{bullets}",
    "am": "🛒 ይህንም ይወዱ ይሆናል:\n{bullets}",
}

DEFAULT_BULLET_FORMAT = "• *{sku}* {name}"


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _default_lifong_catalog_loader(catalog_path: Path) -> dict:
    """Default loader for Lifong stock.json — returns {SKU_UPPER: product_dict}."""
    if not catalog_path.exists():
        return {}
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"upsell_logic catalog load failed: {e}")
        return {}
    out = {}
    skip = {"business_rules", "categories", "escalation_policy"}
    for cat, items in data.items():
        if cat in skip or not isinstance(items, list):
            continue
        for item in items:
            sku = (item.get("sku") or "").upper()
            if sku:
                out[sku] = item
    return out


class UpsellLogic:
    """Configured related-product suggester for a single client."""

    def __init__(self, config: dict):
        # Bundles is the primary configurable signal
        # Normalize to upper-case SKUs everywhere
        raw_bundles = config.get("bundles", {}) or {}
        self.bundles = {
            k.upper(): [s.upper() for s in (v or [])]
            for k, v in raw_bundles.items()
        }

        self.max_suggestions = int(config.get("max_suggestions", 3))
        self.exclude_oos     = bool(config.get("exclude_out_of_stock", True))

        # Catalog (for adding name/status to suggestions)
        loader = config.get("catalog_loader")
        if loader is None:
            catalog_path = config.get("stock_catalog_path")
            if catalog_path:
                cp = Path(catalog_path)
                self._catalog_loader = lambda: _default_lifong_catalog_loader(cp)
            else:
                # No catalog — suggestions will only contain SKUs, no names
                self._catalog_loader = lambda: {}
        elif callable(loader):
            self._catalog_loader = loader
        else:
            raise ValueError("catalog_loader must be callable")

        # Optional: order_history loader for statistical suggestions
        # Should return: [[sku, sku, ...], ...]  (one list per past order)
        self._history_loader = config.get("order_history_loader")

        # Templates (per language)
        self.templates = {**DEFAULT_TEMPLATES, **(config.get("templates", {}) or {})}
        self.bullet_format = config.get("bullet_format", DEFAULT_BULLET_FORMAT)

        # Status-aware filter: callable to check if SKU is in stock (optional)
        # Should return True if in stock. If absent + exclude_oos=True, we don't filter.
        self._is_in_stock = config.get("is_in_stock_callable")

    # ── Internal: pull / enrich ─────────────────────────────────────────────
    def _enrich(self, sku: str, reason: str) -> dict:
        sku_u = sku.upper()
        catalog = self._catalog_loader()
        info = catalog.get(sku_u, {})
        return {
            "sku":    sku_u,
            "name":   info.get("name", ""),
            "reason": reason,
        }

    def _filter_oos(self, suggestions: list) -> list:
        if not self.exclude_oos or self._is_in_stock is None:
            return suggestions
        return [s for s in suggestions if self._is_in_stock(s["sku"])]

    def _dedupe(self, suggestions: list) -> list:
        seen = set()
        out  = []
        for s in suggestions:
            if s["sku"] in seen:
                continue
            seen.add(s["sku"])
            out.append(s)
        return out

    # ── Public API ─────────────────────────────────────────────────────────
    def suggest_for_sku(self, sku: str) -> list:
        """Suggest related SKUs for a single SKU based on bundles config."""
        if not sku:
            return []
        sku_u = sku.upper()
        related = self.bundles.get(sku_u, [])
        suggestions = [self._enrich(s, f"often bought with {sku_u}") for s in related]
        return self._filter_oos(self._dedupe(suggestions))[: self.max_suggestions]

    def suggest_for_history(self, sku_history: list) -> list:
        """Given customer's past SKU enquiries/orders, suggest related SKUs.

        Strategy:
          1. For each past SKU, pull bundles. Count frequency across all past SKUs.
          2. If order_history_loader is set, also use co-occurrence stats.
          3. Exclude SKUs the customer already enquired about.
        """
        if not sku_history:
            return []
        past_set = {s.upper() for s in sku_history if s}

        counter = Counter()

        # 1. Bundle-based contribution
        for past in past_set:
            for related in self.bundles.get(past, []):
                if related not in past_set:
                    counter[related] += 1

        # 2. Statistical co-occurrence from order history (if loader present)
        if self._history_loader:
            try:
                orders = self._history_loader() or []
                for order in orders:
                    order_set = {s.upper() for s in (order or []) if s}
                    overlap = order_set & past_set
                    if overlap:
                        for other in order_set - past_set:
                            counter[other] += 1
            except Exception as e:
                logger.warning(f"upsell_logic history_loader failed: {e}")

        # Build suggestions (top N by frequency)
        ranked = [sku for sku, _count in counter.most_common(self.max_suggestions * 2)]
        suggestions = [self._enrich(s, "matches your previous interest") for s in ranked]
        return self._filter_oos(self._dedupe(suggestions))[: self.max_suggestions]

    def suggest(self, sku: str = None, history: list = None) -> list:
        """Combined suggestion: union of single-SKU bundles + history-based,
        ranked by relevance (single-SKU first), capped at max_suggestions."""
        seen = set()
        out  = []
        for s in (self.suggest_for_sku(sku) if sku else []):
            if s["sku"] not in seen:
                seen.add(s["sku"]); out.append(s)
        for s in (self.suggest_for_history(history) if history else []):
            if s["sku"] not in seen:
                seen.add(s["sku"]); out.append(s)
        return out[: self.max_suggestions]

    def format_suggestion_message(self, suggestions: list, lang: str = "en") -> str:
        """Build a WhatsApp-friendly message from a list of suggestion dicts."""
        if not suggestions:
            return ""
        tpl = self.templates.get(lang) or self.templates.get("en") or "🛒 You might also like:\n{bullets}"
        bullets = "\n".join(
            self.bullet_format.format(sku=s["sku"], name=s["name"] or "", reason=s.get("reason", "")).rstrip()
            for s in suggestions
        )
        return tpl.format(bullets=bullets)


def init(config: dict) -> UpsellLogic:
    config = _expand_env(config)
    return UpsellLogic(config)
