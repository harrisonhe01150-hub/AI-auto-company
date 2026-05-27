"""
restock_waitlist module — public init() factory.

Usage:
    import json
    from modules.restock_waitlist import init as init_restock

    cfg = json.load(open("clients/lifong/restock_waitlist.json"))
    cfg["whatsapp_sender"] = send_whatsapp_message   # injected callable
    waitlist = init_restock(cfg)

    waitlist.add(sku="NC02", waid="27123456789", name="Henry", lang="zh")
    waitlist.notify_for_sku("NC02", "Women's Boat Socks")
    waitlist.remove_all_for_waid("27123456789")
"""
from .module import init, RestockWaitlist, __version__

__all__ = ["init", "RestockWaitlist", "__version__"]
