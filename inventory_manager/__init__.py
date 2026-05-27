"""
inventory_manager module — public init() factory.

Usage:
    from modules.inventory_manager import init as init_inv

    inv = init_inv({
        "ledger_path":        "runtime_data/stock_ledger.json",
        "stock_catalog_path": "inventory/stock.json",
        "low_stock_threshold": 5,
        "on_low_stock":       lambda sku, info, level: alert_managers(...),
        "on_restock":         lambda sku, info: restock_waitlist.notify_for_sku(sku, info["name"]),
    })

    reply = inv.process_command("received 5 bales NC02", recorded_by="harrison")
    balance = inv.get_balance_for("NC02")
"""
from .module import init, InventoryManager, __version__

__all__ = ["init", "InventoryManager", "__version__"]
