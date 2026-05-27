"""
upsell_logic module — public init() factory.

Usage:
    from modules.upsell_logic import init as init_upsell

    upsell = init_upsell({
        "bundles": {
            "NC02": ["NC01", "D1"],
            "HW-40": ["HW-51", "HW-90"],
        },
        "max_suggestions": 3,
        "catalog_loader": lambda: {"NC02": {"name": "..."}, ...},
    })

    suggestions = upsell.suggest_for_sku("NC02")
    msg = upsell.format_suggestion_message(suggestions, lang="en")
"""
from .module import init, UpsellLogic, __version__

__all__ = ["init", "UpsellLogic", "__version__"]
