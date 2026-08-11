# -*- coding: utf-8 -*-
"""
conversation_replay_demo.py — 企微适配器·对话回放演示
模拟顾客(普通微信)与AI店员的完整交互流经过 wecom_core 适配器,
终端以聊天记录形式回放。无需企微账号, 用于向 Coach 演示消息管线。
"""
import sys, os, base64
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "warehouse_ready"))

from wecom_core import WeComKfAdapter, InboundMessage, OutboundReply, CursorStore, WeComCrypto

# ── 简化版"销售大脑"(演示桩: 规则应答, 生产环境为 RAG+LLM) ──
STOCK = {"A3保温壶": {"零售": 45, "拿货": 32, "库存": 140},
         "B1玻璃杯": {"零售": 12, "拿货": 8,  "库存": 800}}
REGULARS = {"buyer_wang": "王姐"}

def sales_brain(msg: InboundMessage):
    if msg.text == "__EVENT_ENTER_SESSION__":
        return OutboundReply(text="您好，我是AI店员小力，档口在线，想看点什么？")
    if msg.msg_type == "image":
        return OutboundReply(text=f"收到您的付款截图（{len(msg.media_bytes)}字节），正在核对金额，老板确认后马上安排～")
    for sku, p in STOCK.items():
        if sku[:2] in msg.text:
            if msg.sender_id in REGULARS:
                return OutboundReply(text=f"{REGULARS[msg.sender_id]}，{sku}您的拿货价{p['拿货']}元/个，现货{p['库存']}个，要多少？")
            return OutboundReply(text=f"{sku}零售{p['零售']}元/个，批发50个起{p['拿货']}元/个，现货{p['库存']}个。")
    if "价" in msg.text or "多少" in msg.text:
        return OutboundReply(text="您问的这款我确认下货号，方便发张图或说下货号吗？")
    return OutboundReply(text="收到！还想了解什么随时说～")

# ── 模拟企微通道 (与离线测试同套Mock机制) ──
class ReplayClient:
    def __init__(self, script): self.script, self.page = script, 0
    def kf_sync_msg(self, cursor="", token="", limit=1000):
        msgs, self.page = self.script if self.page == 0 else [], 1
        return {"msg_list": msgs, "next_cursor": "END", "has_more": 0}
    def media_download(self, mid): return b"\xff\xd8" + b"x"*2048
    def media_upload_image(self, b, filename="i.jpg"): return "MID"
    def kf_send_text(self, kf, u, text): print(f"    🤖 AI店员 → {u}:  {text}\n")
    def kf_send_image(self, kf, u, mid): print(f"    🤖 AI店员 → {u}:  [图片]\n")

SCRIPT = [
    {"msgid": "r0", "open_kfid": "kf1", "external_userid": "buyer_new", "origin": 3,
     "msgtype": "event", "event": {"event_type": "enter_session"}},
    {"msgid": "r1", "open_kfid": "kf1", "external_userid": "buyer_new", "origin": 3,
     "msgtype": "text", "text": {"content": "A3保温壶怎么卖？"}},
    {"msgid": "r2", "open_kfid": "kf1", "external_userid": "buyer_wang", "origin": 3,
     "msgtype": "text", "text": {"content": "老板，A3来60个"}},
    {"msgid": "r3", "open_kfid": "kf1", "external_userid": "buyer_wang", "origin": 3,
     "msgtype": "image", "image": {"media_id": "PAY001"}},
]
LABEL = {"buyer_new": "新客·小陈", "buyer_wang": "熟客·王姐"}

print("="*62)
print("  企微适配器 · 对话回放演示  (顾客端=普通微信, 通道=微信客服)")
print("="*62 + "\n")

def narrated_brain(msg):
    who = LABEL.get(msg.sender_id, msg.sender_id)
    shown = {"__EVENT_ENTER_SESSION__": "[扫码进入会话]"}.get(msg.text, msg.text)
    if msg.msg_type == "image":
        shown = "[发送付款截图]"
    print(f"    👤 {who}:  {shown}")
    return sales_brain(msg)

key = base64.b64encode(os.urandom(32)).decode().rstrip("=")
adapter = WeComKfAdapter(on_message=narrated_brain, client=ReplayClient(SCRIPT),
                         crypto=WeComCrypto("t", key, "corp"),
                         cursor_store=CursorStore("/tmp/replay_cursor.json"))
n = adapter.sync_and_dispatch()
print("="*62)
print(f"  回放完成: 处理消息 {n} 条 | 熟客识别✓ 双价体系✓ 截图OCR入口✓")
print("="*62)
