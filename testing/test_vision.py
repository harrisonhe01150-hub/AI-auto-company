# -*- coding: utf-8 -*-
"""图片识别测试：付款凭证 → 老板端；商品照 → 产品信息；存疑 → 反问 + 下一句补路由。
用 mock 视觉模型，不消耗真实 API。"""
import os, sys, json
os.environ.update(WECOM_CORP_ID="ww", WECOM_KF_SECRET="s", WECOM_TOKEN="t",
                  WECOM_AES_KEY="A"*43, BOSS_KEY="k")
sys.path.insert(0, os.path.abspath("../warehouse_ready")); sys.path.insert(0, os.path.abspath("."))
from pathlib import Path
if Path("dianxiaoli_data.json").exists(): Path("dianxiaoli_data.json").unlink()

import dianxiaoli_core as core
import dianxiaoli_brain as brain
from wecom_core import InboundMessage

P = F = 0
def chk(n, c, e=""):
    global P, F
    P, F = (P+1, F) if c else (P, F+1)
    print(("PASS  " if c else "FAIL  ") + n + ("" if c else "   <- " + str(e)[:200]))

def m(uid, text="", mtype="text", media=None, raw=None):
    return InboundMessage(channel="wecom_kf", msg_id="x", conversation_id=f"kf:{uid}",
                          sender_id=uid, account_id="kf", msg_type=mtype, text=text,
                          media_bytes=media, media_id="", raw=raw or {})

def rules_only():
    """关掉 LLM，走确定性规则路径（下单/问价断言不受 mock 文案影响）"""
    brain._router = brain.LLMRouter()

def say(uid, text):
    rules_only()
    r = core.brain(m(uid, text)); return r.text if r else ""

def send_img(uid, verdict, text_reply="收到", raw=None):
    """verdict = 视觉模型要返回的 JSON dict；同时给文本调用一个安全兜底回复"""
    def mock(system_prompt, messages):
        if "图片分类" in system_prompt:
            return json.dumps(verdict, ensure_ascii=False)
        return text_reply
    brain.set_mock(mock)
    r = core.brain(m(uid, "", "image", media=b"\xff\xd8fakejpeg", raw=raw))
    return r.text if r else ""

def pendings(uid=None):
    d = core.load()
    return [p for p in d["pending"] if uid is None or p["userid"] == uid]

# ── 1. 无视觉能力 → 保持原行为（一律按付款凭证走老板核验）──
brain._router = brain.LLMRouter()
chk("无视觉能力时不启用", not brain._vision_available())
n0 = len(pendings())
r = core.brain(m("v0", "", "image", media=b"img"))
chk("无视觉时兜底为付款凭证", "核对" in r.text, r.text)
chk("无视觉时仍进待核准队列", len(pendings()) == n0 + 1)

# ── 2. 付款凭证 → 老板端待核准，回复不含产品信息 ──
n0 = len(pendings("v1"))
r = send_img("v1", {"type": "payment", "amount": None, "sku": "", "confidence": 0.95, "note": "微信支付成功页"})
chk("付款凭证-回复告知在核对", "核对" in r or "老板" in r, r)
chk("付款凭证-已入待核准", len(pendings("v1")) == n0 + 1)
chk("付款凭证-类型正确", pendings("v1")[-1]["kind"] == "付款核验", pendings("v1")[-1])
chk("付款凭证-图片带给老板", bool(pendings("v1")[-1]["media"]), pendings("v1")[-1])

# ── 3. 付款凭证带金额，与订单一致 → 金额比对 ──
say("v2", "A3保温壶来60个")
d = core.load(); my = [p for p in d["pending"] if p["userid"] == "v2" and p["kind"] == "订单核准"]
chk("先下单成功", len(my) == 1, d["pending"])
amt = my[-1]["amount"]
r = send_img("v2", {"type": "payment", "amount": amt, "sku": "", "confidence": 0.9, "note": "转账截图"})
chk("金额一致-识别出金额", f"{amt:,.0f}" in r, r)
chk("金额一致-一致判定", "一致" in r, r)

# ── 4. 付款凭证金额对不上 → 标记异常给老板，不擅自发货 ──
say("v3", "A3保温壶来60个")
d = core.load(); amt3 = [p for p in d["pending"] if p["userid"] == "v3" and p["kind"] == "订单核准"][-1]["amount"]
r = send_img("v3", {"type": "payment", "amount": amt3 - 500, "sku": "", "confidence": 0.9, "note": "转账截图"})
chk("金额异常-提示有出入", "出入" in r or "差" in r, r)
chk("金额异常-以老板核准为准", "核准" in r, r)
chk("金额异常-队列标记异常", pendings("v3")[-1]["kind"] == "金额异常", pendings("v3")[-1])

# ── 5. 商品照 → 直接报产品信息，不进老板队列 ──
n0 = len(pendings("v4"))
r = send_img("v4", {"type": "product", "amount": None, "sku": "A3", "confidence": 0.88, "note": "银色保温壶"})
chk("商品照-认出商品名", "保温壶" in r, r)
chk("商品照-给了价格", "45" in r, r)
chk("商品照-不进老板队列", len(pendings("v4")) == n0, pendings("v4"))
chk("商品照-不报库存数字", not any(x in r for x in ["140", "件库存", "库存140"]), r)

# ── 6. 商品照进会话记忆 → 下一句只说数量也能接上 ──
r = say("v4", "要100个")
chk("商品照-记忆延续到下一句", "保温壶" in r or "A3" in r, r)

# ── 7. 断货商品照 → 说没货 + 登记，不报数字 ──
r = send_img("v5", {"type": "product", "amount": None, "sku": "G6", "confidence": 0.9, "note": "帆布袋"})
chk("断货商品照-明说没货", "没货" in r or "断货" in r, r)
chk("断货商品照-给登记", "登记" in r or "通知" in r, r)

# ── 8. 商品照认不出型号 → 反问，不瞎报价 ──
r = send_img("v6", {"type": "product", "amount": None, "sku": "", "confidence": 0.8, "note": "一个塑料筐"})
chk("认不出型号-不瞎报价", "¥" not in r, r)
chk("认不出型号-请顾客说型号", "货号" in r or "哪款" in r, r)

# ── 9. 低置信度 → 一律反问，绝不当付款凭证上报 ──
n0 = len(pendings("v7"))
r = send_img("v7", {"type": "payment", "amount": 999, "sku": "", "confidence": 0.3, "note": "看不清"})
chk("低置信度-转为反问", "还是" in r, r)
chk("低置信度-不上报老板", len(pendings("v7")) == n0, pendings("v7"))

# ── 10. other → 反问 + 暂存；顾客答"付款的"后补路由到老板端 ──
n0 = len(pendings("v8"))
r = send_img("v8", {"type": "other", "amount": None, "sku": "", "confidence": 0.9, "note": "风景照"})
chk("其他图片-反问", "付款凭证" in r and "货" in r, r)
chk("其他图片-暂不上报", len(pendings("v8")) == n0)
chk("其他图片-已暂存", bool(core.load()["sessions"]["v8"].get("pending_image")))
r = say("v8", "刚那张是付款的")
chk("补路由-转成付款核验", pendings("v8")[-1]["kind"] == "付款核验" if pendings("v8") else False, pendings("v8"))
chk("补路由-图片没丢", bool(pendings("v8")[-1]["media"]) if pendings("v8") else False)
chk("补路由-暂存已清除", not core.load()["sessions"]["v8"].get("pending_image"))

# ── 11. 视觉模型崩溃 → 不影响接待，兜底到付款流程 ──
def boom(system_prompt, messages):
    raise RuntimeError("vision down")
brain.set_mock(boom)
n0 = len(pendings("v9"))
r = core.brain(m("v9", "", "image", media=b"img"))
chk("视觉崩溃-仍有回复", bool(r and r.text), r)
chk("视觉崩溃-兜底进队列", len(pendings("v9")) == n0 + 1)
chk("视觉崩溃-已记入失败计数", brain.STATS.get("vision_fail", 0) > 0, brain.STATS)

brain._router = brain.LLMRouter()
print(f"\n{'='*46}\n图片识别测试: {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
