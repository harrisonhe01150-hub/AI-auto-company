"""
adapter.py — wecom_core 模块主体 (模块仓库规范: 与 whatsapp_core 平级)

企业微信·微信客服 渠道适配器 — AI 数字店员的中国化通道。

设计原则 (与 WhatsApp 适配器同构):
  销售核心永远不知道自己在和哪个平台说话。
  本文件只做三件事: ①收 (回调→拉取→标准化) ②发 (标准回复→kf API) ③媒体桥接。
  业务核心通过 on_message(InboundMessage) -> Optional[OutboundReply] 挂载。

数据流:
  顾客(普通微信) → 微信客服会话 → 企微回调(加密事件) → [GET/POST /wecom/callback]
    → 验签解密 → kf_msg_or_event 事件携带 Token → kf/sync_msg 游标拉取
    → 逐条标准化为 InboundMessage → on_message(销售大脑) → 回复经 kf/send_msg 发出

游标持久化: 默认 JSON 文件 (单实例 Railway 够用), 生产可换 Redis/DB —— 只需替换 CursorStore.

接入你的主程序 (FastAPI):
    from wecom_core import build_router
    app.include_router(build_router(on_message=sales_brain_entry))
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Request, Response

from .crypto import WeComCrypto, WeComCryptoError
from .client import WeComClient

# ─────────────────────────────────────────────────────────────
# 标准化消息模型 —— 字段命名对齐内部核心, 如与现有模型不一致, 仅改此处映射
# ─────────────────────────────────────────────────────────────

@dataclass
class InboundMessage:
    channel: str                 # 恒为 "wecom_kf"
    msg_id: str
    conversation_id: str         # open_kfid:external_userid — 会话唯一键
    sender_id: str               # external_userid (顾客)
    account_id: str              # open_kfid (客服账号 = 某个商户的店员号)
    msg_type: str                # text / image / voice / file / video / event / unsupported
    text: str = ""               # 文本内容 (voice 场景可后接 ASR)
    media_bytes: Optional[bytes] = None   # 图片等媒体原始字节 (付款截图 → OCR)
    media_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class OutboundReply:
    text: str = ""
    image_bytes: Optional[bytes] = None   # 可选: 回图 (商品图/对账单)


OnMessage = Callable[[InboundMessage], Optional[OutboundReply]]

# ─────────────────────────────────────────────────────────────
# 游标存储
# ─────────────────────────────────────────────────────────────

class CursorStore:
    """kf/sync_msg 的 next_cursor 持久化. 单文件 JSON, 线程安全."""

    def __init__(self, path: str = "wecom_cursor.json"):
        self.path = Path(path)
        self._lock = threading.Lock()

    def get(self) -> str:
        try:
            return json.loads(self.path.read_text()).get("cursor", "")
        except Exception:
            return ""

    def set(self, cursor: str) -> None:
        with self._lock:
            self.path.write_text(json.dumps({"cursor": cursor}))


# ─────────────────────────────────────────────────────────────
# 适配器主体
# ─────────────────────────────────────────────────────────────

class WeComKfAdapter:
    def __init__(self, on_message: OnMessage,
                 client: Optional[WeComClient] = None,
                 crypto: Optional[WeComCrypto] = None,
                 cursor_store: Optional[CursorStore] = None,
                 download_media: bool = True,
                 cold_start_skip_seconds: int = 300,
                 seen_capacity: int = 800):
        """
        cold_start_skip_seconds: 冷启动（游标为空，如容器重启/重新部署后）时，
            早于「启动时刻 - N 秒」的历史消息只推进游标、不再重复应答。
            设 0 可关闭该保护。这解决了「每次部署后 AI 从第一条消息重头回复」的问题。
        seen_capacity: 进程内已处理 msgid 记忆容量，防止同批消息重复分发。
        """
        self.on_message = on_message
        self.client = client or WeComClient()
        self.crypto = crypto or WeComCrypto(
            token=os.environ["WECOM_TOKEN"],
            encoding_aes_key=os.environ["WECOM_AES_KEY"],
            receive_id=os.environ["WECOM_CORP_ID"],
        )
        self.cursors = cursor_store or CursorStore()
        self.download_media = download_media
        self._sync_lock = threading.Lock()
        # 冷启动保护
        self.cold_start_skip_seconds = int(os.environ.get("WECOM_COLD_START_SKIP", cold_start_skip_seconds))
        self._started_at = time.time()
        self._cold_start = not bool(self.cursors.get())   # 启动时游标为空 = 冷启动
        # msgid 去重
        self._seen = OrderedDict()
        self._seen_capacity = seen_capacity
        self.stats = {"dispatched": 0, "skipped_stale": 0, "skipped_dup": 0, "skipped_origin": 0}

    # ── 去重与冷启动判定 ────────────────────────────────────
    def _is_dup(self, msgid: str) -> bool:
        if not msgid:
            return False
        if msgid in self._seen:
            return True
        self._seen[msgid] = 1
        while len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)
        return False

    def _is_stale(self, m: dict) -> bool:
        """冷启动后, 早于启动时刻的历史消息不再重复应答（只推进游标）。"""
        if not self._cold_start or self.cold_start_skip_seconds <= 0:
            return False
        try:
            send_time = float(m.get("send_time") or 0)
        except (TypeError, ValueError):
            return False
        if not send_time:
            return False
        return send_time < (self._started_at - self.cold_start_skip_seconds)

    # ── 回调入口 ────────────────────────────────────────────
    def handle_get(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """企微后台"保存回调配置"时的 URL 验证."""
        return self.crypto.decrypt_url_echo(msg_signature, timestamp, nonce, echostr)

    def handle_post(self, msg_signature: str, timestamp: str, nonce: str, body_xml: str) -> None:
        """收到加密事件 → 解密 → 若为 kf_msg_or_event 则触发一次增量拉取."""
        plain = self.crypto.decrypt_post_xml(msg_signature, timestamp, nonce, body_xml)
        root = ET.fromstring(plain)
        event = (root.findtext("Event") or "").lower()
        if event == "kf_msg_or_event":
            token = root.findtext("Token") or ""
            kfid = root.findtext("OpenKfId") or ""
            self.sync_and_dispatch(event_token=token, open_kfid=kfid)

    # ── 拉取与分发 ──────────────────────────────────────────
    def sync_and_dispatch(self, event_token: str = "", open_kfid: str = "") -> int:
        """游标拉取全部新消息并逐条分发. 返回处理条数. 幂等: 依赖游标推进."""
        handled = 0
        with self._sync_lock:  # 防并发重复拉取
            cursor = self.cursors.get()
            while True:
                resp = self.client.kf_sync_msg(cursor=cursor, token=event_token, open_kfid=open_kfid)
                for m in resp.get("msg_list", []):
                    mid = m.get("msgid", "")
                    if self._is_dup(mid):
                        self.stats["skipped_dup"] += 1
                        continue
                    if self._is_stale(m):
                        self.stats["skipped_stale"] += 1
                        continue
                    try:
                        self._dispatch_one(m)
                        self.stats["dispatched"] += 1
                        handled += 1
                    except Exception as e:  # 单条失败不阻塞游标 (Agent C 会审计日志)
                        print(f"[wecom] dispatch error msgid={m.get('msgid')}: {e}")
                cursor = resp.get("next_cursor", cursor)
                self.cursors.set(cursor)
                if not resp.get("has_more"):
                    break
            self._cold_start = False   # 首轮补齐后恢复正常应答
        return handled

    def _dispatch_one(self, m: dict) -> None:
        msg_type = m.get("msgtype", "unsupported")
        open_kfid = m.get("open_kfid", "")
        external_userid = m.get("external_userid", "")
        if m.get("origin") == 4:   # 4=客服人员发送, 避免机器人自我对话
            self.stats["skipped_origin"] += 1
            return

        text, media_bytes, media_id = "", None, ""
        if msg_type == "text":
            text = m.get("text", {}).get("content", "")
        elif msg_type in ("image", "voice", "file", "video"):
            media_id = m.get(msg_type, {}).get("media_id", "")
            if media_id and self.download_media:
                media_bytes = self.client.media_download(media_id)
        elif msg_type == "event":
            ev = m.get("event", {}).get("event_type", "")
            if ev == "enter_session":   # 顾客扫码进入会话 → 欢迎语交给业务核心
                text = "__EVENT_ENTER_SESSION__"
            else:
                return
        else:
            text = "__EVENT_UNSUPPORTED_MSGTYPE__"

        inbound = InboundMessage(
            channel="wecom_kf", msg_id=m.get("msgid", ""),
            conversation_id=f"{open_kfid}:{external_userid}",
            sender_id=external_userid, account_id=open_kfid,
            msg_type=msg_type, text=text,
            media_bytes=media_bytes, media_id=media_id, raw=m,
        )
        reply = self.on_message(inbound)
        if reply:
            self.send(open_kfid, external_userid, reply)

    # ── 发送 ────────────────────────────────────────────────
    def send(self, open_kfid: str, external_userid: str, reply: OutboundReply) -> None:
        if reply.image_bytes:
            mid = self.client.media_upload_image(reply.image_bytes)
            self.client.kf_send_image(open_kfid, external_userid, mid)
        if reply.text:
            for chunk in _split_text(reply.text, 2000):  # kf 文本上限保护
                self.client.kf_send_text(open_kfid, external_userid, chunk)


def _split_text(s: str, n: int):
    return [s[i:i + n] for i in range(0, len(s), n)] or [s]


# ─────────────────────────────────────────────────────────────
# FastAPI 路由工厂
# ─────────────────────────────────────────────────────────────

def build_router(on_message: OnMessage, adapter: Optional[WeComKfAdapter] = None,
                 path: str = "/wecom/callback") -> APIRouter:
    ad = adapter or WeComKfAdapter(on_message=on_message)
    router = APIRouter()

    @router.get(path)
    async def verify(msg_signature: str, timestamp: str, nonce: str, echostr: str):
        try:
            return Response(content=ad.handle_get(msg_signature, timestamp, nonce, echostr),
                            media_type="text/plain")
        except WeComCryptoError as e:
            return Response(status_code=403, content=str(e))

    @router.post(path)
    async def callback(request: Request, msg_signature: str, timestamp: str, nonce: str):
        body = (await request.body()).decode("utf-8")
        try:
            # 企微要求快速返回; 拉取分发放后台线程, 避免 5s 超时重推
            threading.Thread(target=ad.handle_post,
                             args=(msg_signature, timestamp, nonce, body),
                             daemon=True).start()
            return Response(content="success", media_type="text/plain")
        except WeComCryptoError as e:
            return Response(status_code=403, content=str(e))

    return router
