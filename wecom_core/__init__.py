"""
wecom_crypto.py — 企业微信回调消息加解密 (WXBizMsgCrypt 精简实现)

实现企业微信官方的回调安全机制:
  - 签名校验: dev_msg_signature = sha1(sort(token, timestamp, nonce, encrypt))
  - AES-256-CBC 解密 (key = Base64Decode(EncodingAESKey + "="), iv = key[:16])
  - 明文结构: random(16B) + msg_len(4B, 网络字节序) + msg + receiveid

依赖: pycryptodome  (pip install pycryptodome)
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import time
import os
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES


class WeComCryptoError(Exception):
    pass


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        """
        token / encoding_aes_key: 企微后台回调配置里设置的值
        receive_id: 企业 CorpID (微信客服回调场景为企业 CorpID)
        """
        self.token = token
        self.receive_id = receive_id
        key = base64.b64decode(encoding_aes_key + "=")
        if len(key) != 32:
            raise WeComCryptoError("EncodingAESKey 解码后必须为 32 字节")
        self.key = key
        self.iv = key[:16]

    # ── 签名 ────────────────────────────────────────────────
    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        raw = "".join(sorted([self.token, timestamp, nonce, encrypt]))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def verify(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        return self.signature(timestamp, nonce, encrypt) == msg_signature

    # ── 解密 ────────────────────────────────────────────────
    def decrypt(self, encrypt_b64: str) -> str:
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        plain = cipher.decrypt(base64.b64decode(encrypt_b64))
        pad = plain[-1]
        if isinstance(pad, str):  # py2 兼容习惯, 实际不会走到
            pad = ord(pad)
        if pad < 1 or pad > 32:
            raise WeComCryptoError("非法 PKCS7 填充")
        content = plain[:-pad][16:]  # 去 16 字节随机前缀
        msg_len = struct.unpack("!I", content[:4])[0]
        msg = content[4:4 + msg_len].decode("utf-8")
        recv_id = content[4 + msg_len:].decode("utf-8")
        if recv_id != self.receive_id:
            raise WeComCryptoError(f"receive_id 不匹配: {recv_id!r}")
        return msg

    # ── 加密 (被动回复/本地测试用) ───────────────────────────
    def encrypt(self, msg: str) -> str:
        rnd = os.urandom(16)
        msg_b = msg.encode("utf-8")
        content = rnd + struct.pack("!I", len(msg_b)) + msg_b + self.receive_id.encode("utf-8")
        pad = 32 - (len(content) % 32)
        content += bytes([pad]) * pad
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        return base64.b64encode(cipher.encrypt(content)).decode("utf-8")

    # ── 便捷入口: 处理回调 ───────────────────────────────────
    def decrypt_url_echo(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """GET 回调校验: 校验签名并解出 echostr 明文, 原样返回给企微即完成配置."""
        if not self.verify(msg_signature, timestamp, nonce, echostr):
            raise WeComCryptoError("echostr 签名校验失败")
        return self.decrypt(echostr)

    def decrypt_post_xml(self, msg_signature: str, timestamp: str, nonce: str, body_xml: str) -> str:
        """POST 回调: 从 XML body 提取 Encrypt 字段, 验签并解密, 返回明文 XML."""
        root = ET.fromstring(body_xml)
        encrypt = root.findtext("Encrypt")
        if not encrypt:
            raise WeComCryptoError("回调 XML 缺少 Encrypt 字段")
        if not self.verify(msg_signature, timestamp, nonce, encrypt):
            raise WeComCryptoError("回调签名校验失败")
        return self.decrypt(encrypt)
