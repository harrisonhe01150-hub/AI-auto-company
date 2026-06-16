"""
Lolawe Fashions — end-to-end behaviour test suite (mini Agent C).

Philosophy (Harrison He AI Automation Company, 2026-06-11):
    Every bug we fix MUST leave behind (1) a test that would have caught it and
    (2) a guard inside the module. Bugs are only allowed to happen once.

These tests boot the REAL module stack (modules/ + client configs) with the
Meta API and the LLM mocked out, then replay scripted conversations and assert
on BEHAVIOUR — not just "does it init".

Run:  pytest tests/ -q          (also runs in GitHub Actions on every push)
"""
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Placeholder env so config $-expansion works without real credentials
os.environ.setdefault("LOLAWE_WHATSAPP_TOKEN",    "test-token")
os.environ.setdefault("LOLAWE_PHONE_NUMBER_ID",   "test-phone-id")
os.environ.setdefault("LOLAWE_ANTHROPIC_API_KEY", "test-anthropic")
os.environ.setdefault("LOLAWE_VERIFY_TOKEN",      "test-verify")
os.environ.setdefault("LOLAWE_APP_SECRET",        "test-secret")

APP_SECRET = "test-secret"
OWNER = "27687287099"
CUSTOMER = "27600000001"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
class FakeLLM:
    """Captures what reaches the LLM; replies with a scripted text."""
    def __init__(self):
        self.calls = []
        self.next_reply = "ok"

    def chat(self, *, system_prompt, messages, max_tokens=1000, **kw):
        self.calls.append({"system": system_prompt, "messages": messages})
        return {"text": self.next_reply, "provider": "fake", "model": "fake",
                "input_tokens": 0, "output_tokens": 0, "fallback_reason": None}


@pytest.fixture(scope="module")
def app_module():
    import app as lolawe_app
    assert lolawe_app.LOLAWE is not None, "module stack failed to build"
    return lolawe_app


@pytest.fixture(scope="module")
def client(app_module):
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)


@pytest.fixture()
def harness(app_module):
    """Patch outbound channels + LLM; collect everything the bot tries to send."""
    stack = app_module.LOLAWE
    sent_texts, sent_images = [], []
    fake_llm = FakeLLM()

    orig_text  = stack["wa"].send_text
    orig_img   = stack["image"]._send
    orig_llm   = stack["sales"]._llm

    stack["wa"].send_text  = lambda to, text: sent_texts.append((to, text)) or {"ok": True}
    stack["image"]._send   = lambda to, url, caption="": sent_images.append((to, url)) or {"ok": True}
    stack["sales"]._llm    = fake_llm

    yield {"texts": sent_texts, "images": sent_images, "llm": fake_llm, "stack": stack}

    stack["wa"].send_text = orig_text
    stack["image"]._send  = orig_img
    stack["sales"]._llm   = orig_llm


def meta_payload(text, from_waid=CUSTOMER, msg_id=None, name="Test Customer"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": from_waid, "profile": {"name": name}}],
        "messages": [{"from": from_waid, "id": msg_id or f"wamid.{time.time()}",
                      "type": "text", "text": {"body": text}}],
    }}]}]}


def signed_post(client, payload):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", content=body,
                       headers={"X-Hub-Signature-256": sig})


def drain():
    time.sleep(0.6)   # background threads


# ─────────────────────────────────────────────────────────────────────────────
# 1. Transport security
# ─────────────────────────────────────────────────────────────────────────────
def test_health(client):
    r = client.get("/")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert len(r.json()["modules"]) == 10

def test_webhook_verify_ok(client):
    r = client.get("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=test-verify&hub.challenge=42")
    assert r.status_code == 200 and r.text == "42"

def test_webhook_verify_bad_token(client):
    r = client.get("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=WRONG&hub.challenge=42")
    assert r.status_code == 403

def test_webhook_rejects_unsigned(client):
    r = client.post("/webhooks/whatsapp", content=b'{"entry":[]}')
    assert r.status_code == 403

def test_webhook_rejects_bad_signature(client):
    r = client.post("/webhooks/whatsapp", content=b'{"entry":[]}',
                    headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 403

def test_webhook_accepts_good_signature(client):
    r = signed_post(client, {"entry": []})
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# 2. Static product images
# ─────────────────────────────────────────────────────────────────────────────
def test_product_image_served(client):
    r = client.get("/products/M-60.jpeg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"

def test_all_catalog_image_urls_have_files():
    data = json.load(open(REPO / "inventory" / "stock.json", encoding="utf-8"))
    missing = []
    for v in data.values():
        if not isinstance(v, list):
            continue
        for it in v:
            url = it.get("image_url")
            if url:
                fname = url.rsplit("/", 1)[1]
                if not (REPO / "inventory" / "photos" / fname).exists():
                    missing.append(fname)
    assert not missing, f"image_url without local file: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Knowledge base behaviour
# ─────────────────────────────────────────────────────────────────────────────
def test_catalog_knowledge_complete(app_module):
    kb = app_module.LOLAWE["sales"].knowledge_retriever("anything", 5)
    data = json.load(open(REPO / "inventory" / "stock.json", encoding="utf-8"))
    skus = [it["sku"] for v in data.values() if isinstance(v, list) for it in v]
    for sku in skus:
        assert f"[{sku}]" in kb, f"SKU {sku} missing from knowledge"
    assert "R365" in kb and "R590" in kb            # M-60 dual price
    assert "NEVER reveal exact stock" in kb

def test_catalog_never_leaks_quantities(app_module):
    kb = app_module.LOLAWE["sales"].knowledge_retriever("x", 5)
    for qty_word in ("stock_quantity", "120 pieces", "300 pieces"):
        assert qty_word not in kb

def test_pending_pricing_guarded(app_module):
    kb = app_module.LOLAWE["sales"].knowledge_retriever("x", 5)
    assert "do NOT quote a price" in kb              # Q-22136 / A03


# ─────────────────────────────────────────────────────────────────────────────
# 4. Conversation behaviour (full webhook → reply pipeline)
# ─────────────────────────────────────────────────────────────────────────────
def test_escalation_on_discount(client, harness):
    r = signed_post(client, meta_payload("any discount for bulk?"))
    assert r.status_code == 200
    drain()
    texts = harness["texts"]
    assert any(to == OWNER and "escalation" in msg.lower() for to, msg in texts), "owner not notified"
    assert any(to == CUSTOMER for to, msg in texts), "customer not acked"
    assert harness["llm"].calls == [], "escalation must not reach the LLM"

def test_wholesale_not_escalated(client, harness):
    """Regression: 'sale' substring used to hijack 'wholesale' into escalation."""
    harness["llm"].next_reply = "We do wholesale! Which product?"
    signed_post(client, meta_payload("I want wholesale prices"))
    drain()
    assert len(harness["llm"].calls) == 1, "'wholesale' wrongly escalated (word-boundary bug)"

def test_reply_mentioning_sku_sends_photo(client, harness):
    """Regression: image_sender args were swapped — photos silently never sent."""
    harness["llm"].next_reply = "Here's the *Teddy Winter Coat* [M-60] 📸"
    signed_post(client, meta_payload("show me the teddy coat"))
    drain()
    assert any(to == CUSTOMER and "M-60" in url for to, url in harness["images"]), \
        f"no photo dispatched: {harness['images']}"

def test_history_reaches_llm(client, harness):
    harness["llm"].next_reply = "It's R590 retail [M-60]"
    signed_post(client, meta_payload("how much is the teddy coat", from_waid="27600000002"))
    drain()
    harness["llm"].next_reply = "Black or cream? 😊"
    signed_post(client, meta_payload("yes send photo", from_waid="27600000002"))
    drain()
    assert len(harness["llm"].calls) == 2
    second_call_msgs = harness["llm"].calls[1]["messages"]
    flat = json.dumps(second_call_msgs, ensure_ascii=False)
    assert "teddy coat" in flat, "previous turn missing — conversation history broken"

def test_dedup_same_msg_id(client, harness):
    p = meta_payload("hello", msg_id="wamid.DUPLICATE-1")
    signed_post(client, p)
    signed_post(client, p)
    drain()
    assert len(harness["llm"].calls) <= 1, "duplicate delivery processed twice"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Owner commands
# ─────────────────────────────────────────────────────────────────────────────
def test_owner_balance_command(client, harness):
    signed_post(client, meta_payload("balance M-60", from_waid=OWNER, name="Owner"))
    drain()
    assert any(to == OWNER and "Teddy Winter Coat" in msg for to, msg in harness["texts"])
    assert harness["llm"].calls == [], "owner command must not reach the LLM"

def test_owner_approve_unknown_payment(client, harness):
    signed_post(client, meta_payload("approve PAY-999", from_waid=OWNER, name="Owner"))
    drain()
    assert any(to == OWNER and "not found" in msg for to, msg in harness["texts"])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Module boundary guards (bugs are only allowed once)
# ─────────────────────────────────────────────────────────────────────────────
def test_guard_image_sender_swapped_args(app_module):
    res = app_module.LOLAWE["image"].send_for_text("some long reply text [M-60]", CUSTOMER)
    assert res.get("queued", 0) == 0 and res.get("error"), "swapped args must be rejected loudly"

def test_guard_send_text_swapped_args(app_module):
    res = app_module.LOLAWE["wa"].send_text("Hello! here is your reply", "27600000001")
    assert res.get("error"), "send_text must reject non-phone recipient"

def test_guard_callback_arity_fails_fast():
    from modules.inventory_manager import init as init_inv
    cfg = {"ledger_path": "/tmp/_t_ledger.json",
           "stock_catalog_path": str(REPO / "inventory" / "stock.json"),
           "on_restock": lambda sku, info, level: None}   # WRONG arity (3 instead of 2)
    with pytest.raises(ValueError, match="on_restock"):
        init_inv(cfg)

def test_prompt_has_whatsapp_format_rules(app_module):
    sb = json.load(open(REPO / "sales_brain.json", encoding="utf-8"))
    p = sb["system_prompt"]
    assert "NEVER use markdown tables" in p
    assert "{contexts}" in p and "{knowledge}" in p
    # prompt must format cleanly (no stray braces)
    p.format(business_name="x", contexts="c", knowledge="k")

def test_ledger_shape():
    ledger = json.load(open(REPO / "runtime_data" / "stock_ledger.json", encoding="utf-8"))
    assert isinstance(ledger, dict) and "transactions" in ledger
