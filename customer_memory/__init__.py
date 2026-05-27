"""
customer_memory module — cross-session customer profile storage.

Usage:
    from modules.customer_memory import init as init_mem

    mem = init_mem({"storage_path": "runtime_data/customer_profiles.json",
                    "timezone": "Africa/Johannesburg"})

    profile = mem.load(phone)
    mem.save(phone, {"name": "Henry", "last_seen_date": "2026-05-27"})
    mem.update_from_reply(phone, user_msg, bot_reply, yoyo_message=None)
    ctx = mem.build_context_string(phone)   # injectable into LLM prompt
"""
from .module import init, CustomerMemory, __version__

__all__ = ["init", "CustomerMemory", "__version__"]
