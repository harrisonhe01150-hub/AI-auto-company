"""wecom_core — 企业微信·微信客服渠道模块 (v0.1.0)

模块仓库规范: 与 whatsapp_core 平级的渠道适配模块。
用法: from wecom_core import build_router, WeComKfAdapter, InboundMessage, OutboundReply
"""

__version__ = "0.1.0"

from .adapter import (
    build_router, WeComKfAdapter, CursorStore,
    InboundMessage, OutboundReply,
)
from .client import WeComClient, WeComAPIError
from .crypto import WeComCrypto, WeComCryptoError
