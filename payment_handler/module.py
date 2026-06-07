"""
payment_handler.module — EFT payment proof workflow.

Customer sends a payment screenshot via WhatsApp → vision callable extracts
{amount, reference, bank, sender, date} → entry saved with PAY-XXX ID +
multilingual confirmation queued → manager notified with approve/reject
syntax → on approve, customer gets multilingual confirmation; on reject,
customer gets a polite decline + manager contact.

Safety features:
  - Duplicate reference detection (catches fraudsters reusing same ref/bank)
  - Vision callable injected (client picks Claude vision / GPT / etc.)
  - Manager approve/reject is the FINAL gate (no auto-approve)
  - All money values pass through unchanged — module doesn't compute anything

Reserved 2026-05-27 (Lifong opted out — anti-scam, Yoyo handles manually).
Built as a skeleton ready for the first opt-in client.

Public API (returned by init):
    submit_proof(image_b64, customer_waid, customer_name="", lang="en") -> dict
        # {payment_id, extracted, is_duplicate, customer_reply_sent}

    approve(payment_id, manager_waid)                       -> dict
    reject(payment_id, manager_waid, reason="")             -> dict

    get_pending(payment_id)                                  -> dict | None
    list_pending(status=None)                                -> list[dict]
    is_duplicate_reference(reference, bank=None)             -> bool
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Lock

import pytz

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


# ── Default vision prompt — client can override via config ─────────────────
DEFAULT_VISION_PROMPT = """You are looking at a payment proof screenshot from a customer (likely a bank EFT app receipt or transfer confirmation).

Extract these fields. Return ONLY a JSON object with these exact keys:
  amount        Total transferred, as a number (no currency symbol). Use 0 if unclear.
  reference     The transaction reference / payment ref code shown on the proof.
  bank          Bank name (e.g. "FNB", "Standard Bank", "ABSA", "Capitec", "Nedbank", "TymeBank", "Discovery"). Use "unknown" if unclear.
  sender        Name of the person/account who sent the money. Use "unknown" if unclear.
  date          Date on the proof, formatted YYYY-MM-DD. Use today's date string if unclear.
  recipient     Name of the recipient (who's being paid). Use "unknown" if unclear.

Return ONLY the JSON object. No explanation, no markdown, no extra text."""


# ── Default 4-language notification templates ──────────────────────────────
DEFAULT_TEMPLATES = {
    "submitted": {
        "en": "Got it 👍 — your payment proof is being reviewed by {manager_name}. We'll WhatsApp you the moment it's confirmed.",
        "zh": "收到 👍 — 您的付款凭证已转 {manager_name} 审核，确认后我们立即通过 WhatsApp 通知您。",
        "af": "Ontvang 👍 — jou betaalbewys word deur {manager_name} hersien. Ons stuur 'n WhatsApp boodskap sodra dit bevestig is.",
        "am": "ተቀብለናል 👍 — የክፍያ ማስረጃዎ በ{manager_name} እየታየ ነው። ሲረጋገጥ ወዲያውኑ በ WhatsApp እናሳውቅዎታለን።",
        "pt": "Recebido 👍 — o seu comprovativo de pagamento está a ser analisado por {manager_name}. Avisaremos no WhatsApp assim que for confirmado.",
    },
    "approved": {
        "en": "✅ Payment confirmed! Reference *{reference}* for *R{amount}*. {manager_name} will be in touch shortly to arrange pickup. Thank you 🙏",
        "zh": "✅ 付款已确认！参考号 *{reference}*，金额 *R{amount}*。{manager_name} 稍后会联系您安排取货。感谢 🙏",
        "af": "✅ Betaling bevestig! Verwysing *{reference}* vir *R{amount}*. {manager_name} sal kortliks kontak om optel te reël. Dankie 🙏",
        "am": "✅ ክፍያ ተረጋግጧል! ማጣቀሻ *{reference}* ለ *R{amount}*። {manager_name} ለመውሰድ በቅርቡ ያነጋግራል። እናመሰግናለን 🙏",
        "pt": "✅ Pagamento confirmado! Referência *{reference}* no valor de *R{amount}*. {manager_name} entrará em contacto em breve para combinar a retirada. Obrigado 🙏",
    },
    "rejected": {
        "en": "❌ We couldn't verify this payment ({reason}). Please contact {manager_name} on *{manager_phone}* and we'll sort it out together.",
        "zh": "❌ 这笔付款无法核实 ({reason})。请联系 {manager_name}：*{manager_phone}* 我们一起解决。",
        "af": "❌ Ons kon nie hierdie betaling verifieer nie ({reason}). Kontak {manager_name} by *{manager_phone}* en ons sal dit saam uitsorteer.",
        "am": "❌ ይህን ክፍያ ማረጋገጥ አልቻልንም ({reason})። እባክዎ {manager_name}ን በ *{manager_phone}* ያግኙ።",
        "pt": "❌ Não conseguimos verificar este pagamento ({reason}). Por favor contacte {manager_name} no *{manager_phone}* e resolveremos juntos.",
    },
    "duplicate": {
        "en": "👀 We've already received a payment proof with reference *{reference}*. Please double-check — sending the same proof twice doesn't speed things up. Contact {manager_name} on *{manager_phone}* if you think there's a mistake.",
        "zh": "👀 我们已经收到过参考号 *{reference}* 的付款凭证。请仔细核对 — 重复发送同一凭证不会加快处理。如有疑问联系 {manager_name}：*{manager_phone}*。",
        "af": "👀 Ons het reeds 'n betaalbewys met verwysing *{reference}* ontvang. Kyk asseblief weer. Kontak {manager_name} by *{manager_phone}* as jy dink daar is 'n fout.",
        "am": "👀 ማጣቀሻ *{reference}* ያለው የክፍያ ማስረጃ ቀደም ብለን ተቀብለናል። እባክዎ እንደገና ይመልከቱ። ስህተት ካለ {manager_name}ን በ *{manager_phone}* ያግኙ።",
        "pt": "👀 Já recebemos um comprovativo com a referência *{reference}*. Verifique novamente — enviar o mesmo comprovativo duas vezes não acelera o processo. Contacte {manager_name} no *{manager_phone}* se achar que há algum erro.",
    },
}


def _expand_env(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class PaymentHandler:
    """Configured payment-proof handler for a single client."""

    def __init__(self, config: dict):
        for key in ("storage_path", "business_name", "manager_name", "manager_waid"):
            if key not in config:
                raise ValueError(f"payment_handler: missing required config: {key!r}")

        self.storage_path  = Path(config["storage_path"])
        self.business_name = config["business_name"]
        self.manager_name  = config["manager_name"]
        self.manager_waid  = config["manager_waid"]
        self.manager_phone = config.get("manager_phone", "")  # for display in customer messages
        self.languages     = list(config.get("languages", ["en"]))
        self.timezone      = pytz.timezone(config.get("timezone", "Africa/Johannesburg"))

        # Pluggable templates (deep-merge over defaults)
        client_tpl = config.get("templates", {}) or {}
        self.templates = {
            stage: {**DEFAULT_TEMPLATES.get(stage, {}), **client_tpl.get(stage, {})}
            for stage in ("submitted", "approved", "rejected", "duplicate")
        }

        # Pluggable callables
        self._vision = config.get("vision_callable")     # (image_b64, prompt) -> dict
        self._send   = config.get("whatsapp_sender")     # (to, text) -> dict
        self.vision_prompt = config.get("vision_prompt", DEFAULT_VISION_PROMPT)

        self.duplicate_check = bool(config.get("duplicate_check", True))
        self.id_prefix       = config.get("id_prefix", "PAY")

        self._lock = Lock()

    # ── Storage I/O ────────────────────────────────────────────────────────
    def _load(self) -> list:
        if not self.storage_path.exists():
            return []
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"payment_handler load failed: {e}")
            return []

    def _save(self, entries: list):
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"payment_handler save failed: {e}")

    def _next_id(self, entries: list) -> str:
        """Generate next PAY-NNN id by scanning existing entries."""
        max_n = 0
        for e in entries:
            pid = e.get("id", "")
            if pid.startswith(self.id_prefix + "-"):
                try:
                    n = int(pid.split("-", 1)[1])
                    if n > max_n:
                        max_n = n
                except ValueError:
                    pass
        return f"{self.id_prefix}-{max_n + 1:03d}"

    def _send_msg(self, to: str, text: str):
        if not self._send:
            logger.warning(f"payment_handler: no whatsapp_sender — would have sent to {to}: {text[:60]}")
            return {"error": "no whatsapp_sender configured"}
        try:
            return self._send(to, text)
        except Exception as e:
            logger.error(f"payment_handler send failed: {e}")
            return {"error": str(e)}

    # ── Public API ─────────────────────────────────────────────────────────
    def is_duplicate_reference(self, reference: str, bank: str = None) -> bool:
        if not reference:
            return False
        ref_lower = reference.strip().lower()
        for e in self._load():
            if (e.get("reference") or "").strip().lower() == ref_lower:
                if bank is None or (e.get("bank") or "").strip().lower() == (bank or "").strip().lower():
                    return True
        return False

    def submit_proof(self, image_b64: str, customer_waid: str,
                     customer_name: str = "", lang: str = "en") -> dict:
        """End-to-end: vision extract → dedup check → save → notify manager →
        return summary for the host to forward to the customer."""
        if not self._vision:
            return {"error": "no vision_callable configured — cannot extract payment info"}

        # 1. Vision extraction
        try:
            extracted = self._vision(image_b64, self.vision_prompt)
            if not isinstance(extracted, dict):
                return {"error": f"vision_callable returned non-dict: {type(extracted).__name__}"}
        except Exception as e:
            logger.error(f"payment_handler vision failed: {e}")
            return {"error": f"vision extraction failed: {e}"}

        # Normalise fields the host can rely on
        for k in ("amount", "reference", "bank", "sender", "date", "recipient"):
            extracted.setdefault(k, "unknown")

        # 2. Duplicate check
        is_dup = False
        if self.duplicate_check:
            is_dup = self.is_duplicate_reference(extracted.get("reference"), extracted.get("bank"))

        if is_dup:
            # Don't save a new entry; reply to customer + alert manager casually
            tpl = self.templates["duplicate"].get(lang) or self.templates["duplicate"]["en"]
            customer_msg = tpl.format(
                reference=extracted.get("reference", "?"),
                manager_name=self.manager_name,
                manager_phone=self.manager_phone or self.manager_waid,
            )
            self._send_msg(customer_waid, customer_msg)
            self._send_msg(self.manager_waid,
                           f"👀 Duplicate payment proof from +{customer_waid} — ref {extracted.get('reference')} (bank {extracted.get('bank')}). Already in system.")
            return {
                "is_duplicate": True,
                "extracted":    extracted,
                "customer_reply_sent": True,
            }

        # 3. Save new entry
        with self._lock:
            entries  = self._load()
            new_id   = self._next_id(entries)
            now      = datetime.now(self.timezone)
            entry = {
                "id":              new_id,
                "customer_waid":   customer_waid,
                "customer_name":   customer_name,
                "lang":            lang if lang in self.languages else (self.languages[0] if self.languages else "en"),
                "amount":          extracted.get("amount"),
                "reference":       extracted.get("reference"),
                "bank":            extracted.get("bank"),
                "sender":          extracted.get("sender"),
                "date_on_proof":   extracted.get("date"),
                "recipient":       extracted.get("recipient"),
                "submitted_at":    now.isoformat(),
                "status":          "pending",
                "actioned_by":     None,
                "actioned_at":     None,
                "reject_reason":   None,
            }
            entries.append(entry)
            self._save(entries)

        # 4. Notify customer (acknowledgment) + manager (with approve/reject syntax)
        ack_tpl = self.templates["submitted"].get(entry["lang"]) or self.templates["submitted"]["en"]
        customer_msg = ack_tpl.format(
            manager_name=self.manager_name,
            business_name=self.business_name,
        )
        self._send_msg(customer_waid, customer_msg)

        manager_msg = (
            f"💰 New payment proof — *{new_id}*\n"
            f"From: +{customer_waid} ({customer_name or 'no name'})\n"
            f"Amount: R{entry['amount']}\n"
            f"Reference: {entry['reference']}\n"
            f"Bank: {entry['bank']}\n"
            f"Sender: {entry['sender']}\n"
            f"Date on proof: {entry['date_on_proof']}\n"
            f"Recipient: {entry['recipient']}\n\n"
            f"Reply *approve {new_id}* or *reject {new_id}* (optionally *reject {new_id} reason here*)."
        )
        self._send_msg(self.manager_waid, manager_msg)

        return {
            "payment_id":   new_id,
            "is_duplicate": False,
            "extracted":    extracted,
            "customer_reply_sent": True,
            "manager_notified":    True,
        }

    def approve(self, payment_id: str, manager_waid: str) -> dict:
        with self._lock:
            entries = self._load()
            target = next((e for e in entries if e.get("id") == payment_id), None)
            if not target:
                return {"error": f"Payment {payment_id} not found"}
            if target.get("status") != "pending":
                return {"error": f"Payment {payment_id} already {target.get('status')}"}
            target["status"]      = "approved"
            target["actioned_by"] = manager_waid
            target["actioned_at"] = datetime.now(self.timezone).isoformat()
            self._save(entries)

        # Notify customer in their original language
        tpl = self.templates["approved"].get(target["lang"]) or self.templates["approved"]["en"]
        msg = tpl.format(
            reference=target.get("reference", "?"),
            amount=target.get("amount", "?"),
            manager_name=self.manager_name,
            business_name=self.business_name,
        )
        self._send_msg(target["customer_waid"], msg)
        return {"ok": True, "payment_id": payment_id, "customer_notified": True}

    def reject(self, payment_id: str, manager_waid: str, reason: str = "") -> dict:
        with self._lock:
            entries = self._load()
            target = next((e for e in entries if e.get("id") == payment_id), None)
            if not target:
                return {"error": f"Payment {payment_id} not found"}
            if target.get("status") != "pending":
                return {"error": f"Payment {payment_id} already {target.get('status')}"}
            target["status"]       = "rejected"
            target["actioned_by"]  = manager_waid
            target["actioned_at"]  = datetime.now(self.timezone).isoformat()
            target["reject_reason"] = reason or "please contact us"
            self._save(entries)

        tpl = self.templates["rejected"].get(target["lang"]) or self.templates["rejected"]["en"]
        msg = tpl.format(
            reason=reason or "we need more info",
            manager_name=self.manager_name,
            manager_phone=self.manager_phone or self.manager_waid,
        )
        self._send_msg(target["customer_waid"], msg)
        return {"ok": True, "payment_id": payment_id, "customer_notified": True}

    def get_pending(self, payment_id: str):
        return next((e for e in self._load() if e.get("id") == payment_id), None)

    def list_pending(self, status: str = None) -> list:
        entries = self._load()
        if status is None:
            return entries
        return [e for e in entries if e.get("status") == status]


def init(config: dict) -> PaymentHandler:
    config = _expand_env(config)
    return PaymentHandler(config)
