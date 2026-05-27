"""
payment_handler module — public init() factory.

Usage:
    from modules.payment_handler import init as init_pay

    pay = init_pay({
        "storage_path":    "runtime_data/pending_payments.json",
        "business_name":   "Lifong Wholesale",
        "manager_name":    "Yoyo",
        "manager_waid":    "27845791010",
        "languages":       ["en", "zh", "af", "am"],
        "vision_callable": claude_vision,           # injected
        "whatsapp_sender": send_whatsapp_message,   # injected
        "duplicate_check": True,
    })

    pay.submit_proof(image_b64, customer_waid="27...", customer_name="Henry", lang="zh")
    pay.approve(payment_id="PAY-001", manager_waid="27...")
    pay.reject(payment_id="PAY-002", manager_waid="27...", reason="amount mismatch")
"""
from .module import init, PaymentHandler, __version__

__all__ = ["init", "PaymentHandler", "__version__"]
