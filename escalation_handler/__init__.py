"""
escalation_handler module — public init() factory.

Usage:
    from modules.escalation_handler import init as init_esc

    esc = init_esc({
        "keywords": ["speak to manager", "talk to boss", ...],
        "manager_name":    "Yoyo",
        "manager_contact": "+27 84 579 1010",
        "message_template": "Sure! Please contact {manager_name}: *{manager_contact}* 😊",
    })

    result = esc.check(user_message)
    if result:
        return result   # {"status": "ESCALATED", "message": "..."}
"""
from .module import init, EscalationHandler, __version__

__all__ = ["init", "EscalationHandler", "__version__"]
