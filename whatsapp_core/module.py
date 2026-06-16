"""
whatsapp_core.module — Meta WhatsApp Cloud API client.

Wraps the send/receive operations every WhatsApp-driven AI agent needs:
text/image/document send, image/document/audio download, and voice transcription
(via OpenAI Whisper).

Originally lived in src/messaging.py. Extracted into the warehouse on 2026-05-27.
All credentials are injected at init time (typically via $env placeholders in
the client config JSON).

Public API (returned by init):
    send_text(to, text)                                -> dict
    send_image_url(to, image_url, caption="")          -> dict
    send_document(to, doc_bytes, filename, caption="") -> dict
    download_media_url(media_id)                       -> str | None
    download_image(media_id)                           -> str | None  (base64 data URI)
    download_document(media_id)                        -> bytes | None
    transcribe_voice(media_id)                         -> str | None
"""
import base64
import logging
import os
import tempfile
import urllib.request

import requests

__version__ = "0.1.1"

logger = logging.getLogger(__name__)


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _valid_waid(to) -> bool:
    """Guard (v0.1.1): recipient must look like a WhatsApp ID (digits, 7-15).
    Catches swapped-argument bugs at the boundary instead of silently no-oping."""
    s = str(to or "").strip().lstrip("+")
    return s.isdigit() and 7 <= len(s) <= 15


class WhatsAppCore:
    """A configured Meta WhatsApp Cloud API client for a single business number."""

    def __init__(self, config: dict):
        for key in ("whatsapp_token", "phone_number_id"):
            if key not in config or not config[key]:
                raise ValueError(f"whatsapp_core: missing required config: {key!r}")

        self.token = config["whatsapp_token"]
        self.phone_number_id = config["phone_number_id"]
        self.openai_api_key = config.get("openai_api_key") or ""

        self.api_version = config.get("graph_api_version", "v19.0")
        self.base = f"https://graph.facebook.com/{self.api_version}"
        self.default_timeout = int(config.get("default_timeout", 10))

    # ── Send: text ─────────────────────────────────────────────────────────
    def send_text(self, to: str, text: str) -> dict:
        if not _valid_waid(to):
            logger.error(f"send_text REJECTED: 'to' is not a phone number: {str(to)[:60]!r} — argument order swapped?")
            return {"error": "invalid recipient — check argument order (to, text)"}
        url = f"{self.base}/{self.phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        try:
            r = requests.post(url, headers=headers, json=data, timeout=self.default_timeout)
            if r.status_code not in (200, 201):
                logger.error(f"send_text HTTP {r.status_code} to={to}: {r.text[:300]}")
                return {"error": f"HTTP {r.status_code}", "body": r.text[:300]}
            logger.info(f"send_text OK to={to} resp={r.json()}")
            return r.json()
        except Exception as e:
            logger.error(f"send_text failed: {e}")
            return {"error": str(e)}

    # ── Send: image by URL ─────────────────────────────────────────────────
    def send_image_url(self, to: str, image_url: str, caption: str = "") -> dict:
        if not _valid_waid(to):
            logger.error(f"send_image_url REJECTED: 'to' is not a phone number: {str(to)[:60]!r} — argument order swapped?")
            return {"error": "invalid recipient — check argument order (to, image_url, caption)"}
        phone = (to or "").strip().lstrip("+")
        url = f"{self.base}/{self.phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        data = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "image",
            "image": {"link": image_url, "caption": caption},
        }
        try:
            r = requests.post(url, headers=headers, json=data, timeout=self.default_timeout)
            if r.status_code not in (200, 201):
                logger.error(f"send_image_url HTTP {r.status_code}: {r.text[:200]}")
                return {"error": f"HTTP {r.status_code}"}
            return r.json()
        except Exception as e:
            logger.error(f"send_image_url failed: {e}")
            return {"error": str(e)}

    # ── Send: document (PDF/xlsx/etc) ──────────────────────────────────────
    def send_document(self, to: str, doc_bytes: bytes, filename: str, caption: str = "") -> dict:
        if not _valid_waid(to):
            logger.error(f"send_document REJECTED: 'to' is not a phone number: {str(to)[:60]!r} — argument order swapped?")
            return {"error": "invalid recipient — check argument order (to, doc_bytes, filename)"}
        phone = (to or "").strip().lstrip("+")
        try:
            # Step 1: upload to media API
            media_url = f"{self.base}/{self.phone_number_id}/media"
            headers   = {"Authorization": f"Bearer {self.token}"}
            mime      = "application/pdf" if filename.lower().endswith(".pdf") else \
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            files     = {"file": (filename, doc_bytes, mime)}
            form      = {"messaging_product": "whatsapp"}
            r = requests.post(media_url, headers=headers, files=files, data=form, timeout=30)
            if r.status_code not in (200, 201):
                logger.error(f"document upload HTTP {r.status_code}: {r.text[:200]}")
                return {"error": f"Upload failed HTTP {r.status_code}"}
            media_id = r.json().get("id")
            if not media_id:
                return {"error": "No media_id from Meta"}

            # Step 2: send document
            msg_url  = f"{self.base}/{self.phone_number_id}/messages"
            msg_hdrs = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            msg_data = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "document",
                "document": {"id": media_id, "caption": caption, "filename": filename},
            }
            r2 = requests.post(msg_url, headers=msg_hdrs, json=msg_data, timeout=self.default_timeout)
            if r2.status_code not in (200, 201):
                return {"error": f"Send failed HTTP {r2.status_code}"}
            return r2.json()
        except Exception as e:
            logger.error(f"send_document failed: {e}")
            return {"error": str(e)}

    # ── Download: media URL ────────────────────────────────────────────────
    def download_media_url(self, media_id: str):
        if not media_id:
            return None
        try:
            url     = f"{self.base}/{media_id}"
            headers = {"Authorization": f"Bearer {self.token}"}
            r = requests.get(url, headers=headers, timeout=self.default_timeout)
            if r.status_code != 200:
                logger.error(f"media_url fetch HTTP {r.status_code}: {r.text[:200]}")
                return None
            return r.json().get("url")
        except Exception as e:
            logger.error(f"download_media_url failed: {e}")
            return None

    # ── Download: image as base64 data URI ─────────────────────────────────
    def download_image(self, media_id: str):
        media_url = self.download_media_url(media_id)
        if not media_url:
            return None
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            r = requests.get(media_url, headers=headers, timeout=15)
            if r.status_code != 200 or len(r.content) < 100:
                logger.error(f"image download HTTP {r.status_code}, size={len(r.content)}")
                return None
            ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            if not ctype.startswith("image/"):
                ctype = "image/jpeg"
            b64 = base64.b64encode(r.content).decode("utf-8")
            return f"data:{ctype};base64,{b64}"
        except Exception as e:
            logger.error(f"download_image failed: {e}")
            return None

    # ── Download: document as raw bytes ────────────────────────────────────
    def download_document(self, media_id: str):
        media_url = self.download_media_url(media_id)
        if not media_url:
            return None
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            r = requests.get(media_url, headers=headers, timeout=30)
            if r.status_code != 200 or len(r.content) < 100:
                logger.error(f"document download HTTP {r.status_code}, size={len(r.content)}")
                return None
            return r.content
        except Exception as e:
            logger.error(f"download_document failed: {e}")
            return None

    # ── Transcribe voice via Whisper ───────────────────────────────────────
    def transcribe_voice(self, media_id: str):
        if not self.openai_api_key:
            logger.warning("openai_api_key missing — cannot transcribe voice")
            return None
        media_url = self.download_media_url(media_id)
        if not media_url:
            return None
        try:
            import openai
            headers_dl = {"Authorization": f"Bearer {self.token}"}
            req = urllib.request.Request(media_url, headers=headers_dl)
            with urllib.request.urlopen(req, timeout=30) as resp:
                audio_bytes  = resp.read()
                content_type = resp.headers.get("Content-Type", "audio/ogg")
            ext = "ogg"
            if "mpeg" in content_type or "mp3" in content_type:
                ext = "mp3"
            elif "mp4" in content_type:
                ext = "mp4"
            elif "webm" in content_type:
                ext = "webm"
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            client = openai.OpenAI(api_key=self.openai_api_key)
            with open(tmp_path, "rb") as af:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1", file=af, response_format="text"
                )
            os.unlink(tmp_path)
            return str(transcript).strip() if transcript else None
        except Exception as e:
            logger.error(f"transcribe_voice failed: {e}")
            return None


def init(config: dict) -> WhatsAppCore:
    config = _expand_env(config)
    return WhatsAppCore(config)
