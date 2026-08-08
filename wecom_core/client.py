"""
wecom_client.py — 企业微信 API 客户端 (微信客服 kf 专用)

覆盖 AI 数字店员所需的最小 API 面:
  - access_token 获取与缓存 (7200s, 提前 300s 刷新)
  - kf/sync_msg     游标式拉取顾客消息 (微信客服的收信模式是"拉", 不是推正文)
  - kf/send_msg     发送文本/图片回复
  - media 上传/下载  (顾客发来的付款截图 media_id -> bytes, 供 OCR)

依赖: requests
环境变量 (或构造参数):
  WECOM_CORP_ID       企业 CorpID
  WECOM_KF_SECRET     微信客服 Secret (企微后台-微信客服-API)
"""

from __future__ import annotations

import os
import time
import threading
from typing import Optional

import requests

API = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComAPIError(Exception):
    def __init__(self, errcode: int, errmsg: str, where: str = ""):
        self.errcode, self.errmsg, self.where = errcode, errmsg, where
        super().__init__(f"[{where}] errcode={errcode} errmsg={errmsg}")


class WeComClient:
    def __init__(self, corp_id: Optional[str] = None, secret: Optional[str] = None,
                 timeout: int = 15):
        self.corp_id = corp_id or os.environ["WECOM_CORP_ID"]
        self.secret = secret or os.environ["WECOM_KF_SECRET"]
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._lock = threading.Lock()

    # ── token ───────────────────────────────────────────────
    def access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry - 300:
                return self._token
            r = requests.get(f"{API}/gettoken",
                             params={"corpid": self.corp_id, "corpsecret": self.secret},
                             timeout=self.timeout).json()
            if r.get("errcode"):
                raise WeComAPIError(r["errcode"], r.get("errmsg", ""), "gettoken")
            self._token = r["access_token"]
            self._token_expiry = time.time() + int(r.get("expires_in", 7200))
            return self._token

    def _post(self, path: str, payload: dict, where: str) -> dict:
        r = requests.post(f"{API}/{path}", params={"access_token": self.access_token()},
                          json=payload, timeout=self.timeout).json()
        if r.get("errcode") not in (0, None):
            if r["errcode"] in (40014, 42001):  # token 失效, 强刷重试一次
                with self._lock:
                    self._token = None
                r = requests.post(f"{API}/{path}", params={"access_token": self.access_token()},
                                  json=payload, timeout=self.timeout).json()
                if r.get("errcode") not in (0, None):
                    raise WeComAPIError(r["errcode"], r.get("errmsg", ""), where)
            else:
                raise WeComAPIError(r["errcode"], r.get("errmsg", ""), where)
        return r

    # ── 微信客服: 拉取消息 ───────────────────────────────────
    def kf_sync_msg(self, cursor: str = "", token: str = "", limit: int = 1000) -> dict:
        """
        拉取顾客消息. 回调事件里带的 Token 用于首次定位, 之后凭 next_cursor 增量拉.
        返回: {"msg_list": [...], "next_cursor": "...", "has_more": 0/1}
        """
        payload: dict = {"limit": limit}
        if cursor:
            payload["cursor"] = cursor
        if token:
            payload["token"] = token
        if open_kfid:
            payload["open_kfid"] = open_kfid
        return self._post("kf/sync_msg", payload, "kf/sync_msg")

    # ── 微信客服: 发送消息 ───────────────────────────────────
    def kf_send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
        return self._post("kf/send_msg", {
            "touser": external_userid, "open_kfid": open_kfid,
            "msgtype": "text", "text": {"content": text},
        }, "kf/send_msg(text)")

    def kf_send_image(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
        return self._post("kf/send_msg", {
            "touser": external_userid, "open_kfid": open_kfid,
            "msgtype": "image", "image": {"media_id": media_id},
        }, "kf/send_msg(image)")

    # ── 媒体 ────────────────────────────────────────────────
    def media_download(self, media_id: str) -> bytes:
        """下载顾客发来的媒体 (付款截图 -> bytes -> 交给 payment-handler OCR)."""
        r = requests.get(f"{API}/media/get",
                         params={"access_token": self.access_token(), "media_id": media_id},
                         timeout=self.timeout)
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype:  # 出错时返回 json
            j = r.json()
            raise WeComAPIError(j.get("errcode", -1), j.get("errmsg", ""), "media/get")
        return r.content

    def media_upload_image(self, image_bytes: bytes, filename: str = "img.jpg") -> str:
        """上传图片素材 (商品图/报表图发给顾客), 返回 media_id, 有效期 3 天."""
        r = requests.post(f"{API}/media/upload",
                          params={"access_token": self.access_token(), "type": "image"},
                          files={"media": (filename, image_bytes)},
                          timeout=self.timeout).json()
        if r.get("errcode") not in (0, None):
            raise WeComAPIError(r["errcode"], r.get("errmsg", ""), "media/upload")
        return r["media_id"]
