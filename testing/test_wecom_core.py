"""
test_wecom_core.py — 离线自测 (无需企微账号/网络)

覆盖:
  1. 加解密回环: encrypt -> decrypt 还原; 签名校验
  2. GET 回调 echostr 流程
  3. POST 回调 -> 事件解析 -> sync_msg 拉取(mock) -> 标准化分发 -> 回复(mock)
  4. 媒体消息: image media 下载(mock) -> media_bytes 进入 InboundMessage
  5. 游标推进与 has_more 翻页

运行: python test_wecom_core.py
"""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wecom_core import WeComCrypto
from wecom_core import WeComKfAdapter, InboundMessage, OutboundReply, CursorStore

TOKEN = "testtoken123"
AES_KEY = base64.b64encode(os.urandom(32)).decode().rstrip("=")  # 43 字符
CORP_ID = "ww_test_corp"

crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
passed, failed = 0, 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


# ── 1. 加解密回环 ──────────────────────────────────────────
plain_xml = "<xml><Event>kf_msg_or_event</Event><Token>EVTTOKEN42</Token></xml>"
enc = crypto.encrypt(plain_xml)
check("encrypt->decrypt 回环", crypto.decrypt(enc) == plain_xml)
sig = crypto.signature("1690000000", "nonce1", enc)
check("签名校验通过", crypto.verify(sig, "1690000000", "nonce1", enc))
check("篡改签名被拒", not crypto.verify(sig, "1690000001", "nonce1", enc))

# ── 2. GET echostr ────────────────────────────────────────
echo_plain = "3061783169"
echo_enc = crypto.encrypt(echo_plain)
echo_sig = crypto.signature("1690000002", "n2", echo_enc)
check("GET 回调 echostr 解密", crypto.decrypt_url_echo(echo_sig, "1690000002", "n2", echo_enc) == echo_plain)

# ── 3+4+5. POST -> sync -> dispatch (mock client) ─────────
FAKE_IMAGE = b"\xff\xd8\xff fake-jpeg-bytes(payment-screenshot)"

class MockClient:
    """模拟 kf/sync_msg 两页数据 + 媒体下载 + 发送记录"""
    def __init__(self):
        self.sent_texts, self.sent_images, self.sync_calls = [], [], []

    def kf_sync_msg(self, cursor="", token="", limit=1000):
        self.sync_calls.append((cursor, token))
        if cursor == "":
            return {"msg_list": [
                {"msgid": "m1", "open_kfid": "kfA", "external_userid": "buyer_wang",
                 "origin": 3, "msgtype": "text", "text": {"content": "A3保温壶60个什么价"}},
                {"msgid": "m2", "open_kfid": "kfA", "external_userid": "buyer_wang",
                 "origin": 3, "msgtype": "image", "image": {"media_id": "MEDIA_PAY_001"}},
            ], "next_cursor": "CUR_1", "has_more": 1}
        return {"msg_list": [
            {"msgid": "m3", "open_kfid": "kfA", "external_userid": "buyer_li",
             "origin": 3, "msgtype": "event",
             "event": {"event_type": "enter_session"}},
            {"msgid": "m4", "open_kfid": "kfA", "external_userid": "buyer_li",
             "origin": 4, "msgtype": "text", "text": {"content": "客服自己发的,应跳过"}},
        ], "next_cursor": "CUR_2", "has_more": 0}

    def media_download(self, media_id):
        assert media_id == "MEDIA_PAY_001"
        return FAKE_IMAGE

    def media_upload_image(self, image_bytes, filename="img.jpg"):
        self.sent_images.append(("UPLOADED", len(image_bytes)))
        return "MEDIA_OUT_1"

    def kf_send_text(self, open_kfid, external_userid, text):
        self.sent_texts.append((open_kfid, external_userid, text))
        return {"errcode": 0}

    def kf_send_image(self, open_kfid, external_userid, media_id):
        self.sent_images.append((open_kfid, external_userid, media_id))
        return {"errcode": 0}


received: list[InboundMessage] = []

def sales_brain(msg: InboundMessage):
    """模拟销售大脑: 文本→报价回复; 图片(付款截图)→确认回复; 进场事件→欢迎语"""
    received.append(msg)
    if msg.msg_type == "text":
        return OutboundReply(text=f"[报价] 已收到: {msg.text}")
    if msg.msg_type == "image":
        assert msg.media_bytes == FAKE_IMAGE, "媒体字节应已下载注入"
        return OutboundReply(text="[OCR] 收到付款截图, 核对中")
    if msg.text == "__EVENT_ENTER_SESSION__":
        return OutboundReply(text="您好, 我是AI店员, 想看点什么?")
    return None


mock = MockClient()
cursor_file = "/tmp/test_wecom_cursor.json"
if os.path.exists(cursor_file):
    os.remove(cursor_file)

adapter = WeComKfAdapter(on_message=sales_brain, client=mock,
                         crypto=crypto, cursor_store=CursorStore(cursor_file))

post_body = f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
adapter.handle_post(sig, "1690000000", "nonce1", post_body)

check("翻页拉取: sync 被调用 2 次", len(mock.sync_calls) == 2)
check("事件 Token 传入首次拉取", mock.sync_calls[0][1] == "EVTTOKEN42")
check("origin=4 客服消息被跳过", len(received) == 3)
check("文本消息标准化", received[0].text == "A3保温壶60个什么价" and received[0].conversation_id == "kfA:buyer_wang")
check("付款截图 media 下载注入", received[1].media_bytes == FAKE_IMAGE)
check("enter_session 事件转欢迎语", received[2].text == "__EVENT_ENTER_SESSION__")
check("三条回复全部发出", len(mock.sent_texts) == 3)
check("游标持久化推进到 CUR_2", CursorStore(cursor_file).get() == "CUR_2")

print(f"\n结果: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
