# -*- coding: utf-8 -*-
"""scenario_gate_factory.py — 工厂行业包 · Agent C 上线门禁（工厂场景 22 条）

与 scenario_gate.py 同一套写法：每个场景一个函数，fresh() 重置，断言看回复文本 + 台账。
工厂包通过 d["factory"]["enabled"] 打开；本门禁同时验证「关掉时对档口逻辑零影响」。
"""
import os, sys, json
os.environ.setdefault("WECOM_CORP_ID", "wwtest"); os.environ.setdefault("WECOM_KF_SECRET", "s")
os.environ.setdefault("WECOM_TOKEN", "t"); os.environ.setdefault("WECOM_AES_KEY", "A"*43)
os.environ["BOSS_KEY"] = os.environ.get("BOSS_KEY", "xiaoli888")
os.environ.setdefault("LLM_ENABLED", "0"); os.environ.setdefault("AUDIT_SCHEDULER", "0")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import dianxiaoli_core as dx
import dianxiaoli_brain as brain
import factory_pack as fp
from wecom_core import InboundMessage
from pathlib import Path

SENT = []
dx.set_notifier(lambda kf, u, t: SENT.append((kf, u, t)))

def fresh(factory=True):
    if Path("dianxiaoli_data.json").exists(): Path("dianxiaoli_data.json").unlink()
    if factory:
        d = dx.load(); d["factory"] = {"enabled": True, "contact": "Eng. Zhou"}; dx.save(d)
    SENT.clear()

def m(uid, text="", mtype="text", raw=None):
    return InboundMessage(channel="wecom_kf", msg_id="x", conversation_id=f"kf:{uid}",
                          sender_id=uid, account_id="kfF", msg_type=mtype, text=text,
                          media_bytes=b"\xff\xd8" if mtype == "image" else None, media_id="", raw=raw or {})

def say(uid, text):
    r = dx.brain(m(uid, text)); return r.text if r else ""

def img(uid, vision=None):
    """vision: 模拟 Claude 视觉分类结果（None = 无视觉能力）"""
    if vision is None:
        brain.set_mock(None)
    else:
        brain.set_mock(lambda prompt, msgs: json.dumps(vision))
    os.environ["LLM_ENABLED"] = "1" if vision else "0"
    brain.LLM_ENABLED = bool(vision)
    try:
        r = dx.brain(m(uid, "", "image")); return r.text if r else ""
    finally:
        brain.set_mock(None); brain.LLM_ENABLED = False; os.environ["LLM_ENABLED"] = "0"

def boss(text):
    dx.brain(m("boss", "绑定老板 " + os.environ["BOSS_KEY"]))
    r = dx.brain(m("boss", text)); return r.text if r else ""

def pend(kind=None):
    return [p for p in dx.load()["pending"] if kind is None or p["kind"] == kind]

def has(t, *k): return all(x in t for x in k)
def hasnt(t, *k): return all(x not in t for x in k)

RESULTS = []
def scenario(sid, title, fn, factory=True):
    fresh(factory)
    try:
        ok, why = fn()
    except Exception as e:
        ok, why = False, f"EXC {type(e).__name__}: {e}"
    RESULTS.append((sid, title, ok, why))
    print(("PASS" if ok else "FAIL") + f"  {sid}  {title}" + ("" if ok else f"   <- {str(why)[:150]}"))

# 目录里 A3 拿货价 32 → FOB = 32/7.2*1.22 = 5.42；≥2000 → ×0.98 = 5.31；≥5000 → ×0.95 = 5.15
# ── A. FOB 阶梯报价 ──
def f001():
    t = say("b1", "Hi, what's your FOB price for 500 pcs of the A3 flask?")
    return has(t, "FOB", "5.42", "Ningbo", "500") and hasnt(t, "¥", "140"), t
scenario("f-001", "FOB 报价-起订量档", f001)

def f002():
    t = say("b1", "FOB price for 5000 pcs A3, port Shenzhen")
    return has(t, "5.15", "Shenzhen", "5,000") and hasnt(t, "5.42"), t
scenario("f-002", "FOB 报价-大量档打折+指定港口", f002)

def f003():
    t = say("b1", "A3 保温壶 2000 个 FOB 宁波什么价")
    return has(t, "5.31", "Ningbo", "2,000") and hasnt(t, "¥32"), t
scenario("f-003", "FOB 报价-中文询盘", f003)

def f004():
    t = say("b1", "FOB price for 100 pcs A3?")
    return has(t, "MOQ", "500") and hasnt(t, "total US$"), t
scenario("f-004", "FOB 低于起订量-给起订价和替代路径", f004)

def f005():
    t = say("b1", "Can you quote FOB?")
    return has(t, "SKU", "quantity", "port") and "MOQ" in t, t
scenario("f-005", "FOB 无款号-引导补信息不瞎报", f005)

def f006():
    d = dx.load()
    d["custom_skus"] = [{"sku": "P1", "name": "P1 工业阀门", "retail": 300, "trade": 200, "stock": 500,
                         "fob_tiers": [{"min": 100, "usd": 40}, {"min": 1000, "usd": 36}]}]
    dx.save(d)
    t = say("b1", "FOB for 1000 pcs P1 valve")
    return has(t, "36", "1,000") and hasnt(t, "40.0", "33."), t
scenario("f-006", "SKU 自带 FOB 阶梯表优先于推算", f006)

def f007():
    t1 = say("b1", "A3 FOB 1000 pcs?"); t2 = say("b1", "and lead time?")
    return has(t1, "5.42") and has(t2, "days", "1,000"), t1 + " || " + t2
scenario("f-007", "会话记忆-交期沿用刚聊的款和量", f007)

# ── B. 打样 / 交期 / 报价单 ──
def f010():
    t = say("b2", "Can I get a sample of A3 first?")
    ok = has(t, "US$30", "7 days") and len(pend("样品单")) == 1
    return ok, t
scenario("f-010", "打样-登记样品单+样品费+抵扣", f010)

def f011():
    t = say("b2", "能先寄个样品吗")
    return has(t, "样品费", "抵扣") and "哪款" in t, t
scenario("f-011", "打样-无款号先问款", f011)

def f012():
    t = say("b2", "How long for 3000 pcs of A3?")
    return has(t, "25 days", "3,000"), t
scenario("f-012", "交期-按数量档回复", f012)

def f013():
    t = say("b2", "8000 件 A3 交期多久")
    return has(t, "40 天", "8,000"), t
scenario("f-013", "交期-中文大量档", f013)

def f014():
    t = say("b2", "Please send me a quotation for 2000 pcs A3 FOB Shanghai")
    ok = has(t, "Quotation #", "5.31", "Shanghai", "10,620.00", "30% deposit") and len(pend("报价单")) == 1
    return ok, t
scenario("f-014", "报价单-结构化 PI 草稿进老板待办", f014)

def f015():
    t = say("b2", "给我一份 2000 个 A3 的报价单")
    return has(t, "报价单 #", "US$5.31", "定金") and len(pend("报价单")) == 1, t
scenario("f-015", "报价单-中文", f015)

# ── C. 规格图分流（三分流第四路） ──
def f020():
    t = img("b3", {"type": "spec", "confidence": 0.9, "sku": "", "note": "CAD drawing with dimensions"})
    return has(t, "engineer", "24 hours") and len(pend("转工程")) == 1 and pend("转工程")[0]["media"], t
scenario("f-020", "规格图-转工程不报价（英文）", f020)

def f021():
    t = img("b3", {"type": "spec", "confidence": 0.85, "sku": "", "note": "带尺寸标注的图纸"})
    return has(t, "工程", "24 小时") and len(pend("转工程")) == 1 and hasnt(t, "¥", "US$"), t
scenario("f-021", "规格图-转工程不报价（中文）", f021)

def f022():
    t = img("b3", {"type": "spec", "confidence": 0.4, "sku": "", "note": "blurry"})
    return ("付款凭证" in t and "问" in t) and len(pend("转工程")) == 0, t
scenario("f-022", "规格图-低置信度不猜，反问", f022)

def f023():
    t = img("b3", {"type": "product", "confidence": 0.9, "sku": "A3", "note": "flask photo"})
    return has(t, "A3") and len(pend("转工程")) == 0, t
scenario("f-023", "商品照-仍走原报价路，不误判成规格图", f023)

# ── D. 发货单号推送 ──
def _order(uid="b4", txt="A3来60个"):
    say(uid, txt)
    pid = [p for p in dx.load()["pending"] if p["userid"] == uid][-1]["id"]
    d = dx.load(); dx._do_act(d, pid, "approve"); dx.save(d)
    return pid

def f030():
    pid = _order()
    t = boss(f"{pid} 发货 SF1234567890")
    o = next(o for o in dx.load()["orders"] if o["id"] == pid)
    ok = has(t, "已记发货", "顺丰") and o.get("tracking") == "SF1234567890"
    ok = ok and bool(SENT) and SENT[-1][1] == "b4" and "SF1234567890" in SENT[-1][2] and SENT[-1][0] == "kfF"
    return ok, (t, SENT[-1:])
scenario("f-030", "老板发单号-台账记录+自动推给买家", f030)

def f031():
    pid = _order()
    t = boss(f"发货 #{pid} YT9876543210")
    return has(t, "圆通") and any("YT9876543210" in s[2] for s in SENT), t
scenario("f-031", "老板发单号-另一种语序+承运商识别", f031)

def f032():
    t = boss("999 发货 SF0000000001")
    return has(t, "没找到", "999") and not SENT, t
scenario("f-032", "老板发单号-单号只能挂在已核准订单", f032)

def f033():
    pid = _order()
    t1 = say("b4", "shipped yet?")
    boss(f"{pid} 发货 SF1234567890")
    t2 = say("b4", "tracking number please")
    return has(t1, "not shipped yet") and has(t2, "SF1234567890", "顺丰"), t1 + " || " + t2
scenario("f-033", "买家问物流-发货前后从台账答", f033)

def f034():
    t = say("b5", "我的货发了吗")
    return has(t, "还没有已确认") or has(t, "没有已确认"), t
scenario("f-034", "买家问物流-无订单不编造", f034)

# ── E. 关掉工厂包时零影响 ──
def f040():
    t = say("b6", "Hi, what's your FOB price for 500 pcs of the A3 flask?")
    return has(t, "FOB", "Ningbo/Yiwu") and hasnt(t, "500+ pcs", "Say the word"), t   # 原有档口英文分支的措辞
scenario("f-040", "factory.enabled=false → 走原有义乌外贸分支", f040, factory=False)

def f041():
    pid = _order("b7")
    t = boss(f"{pid} 发货 SF1234567890")
    return has(t, "已记发货") and bool(SENT), t
scenario("f-041", "发货单号推送在档口版同样可用", f041, factory=False)

# ── F. 老板开关 ──
def f050():
    t = boss("开启工厂模式")
    ok = "工厂模式已开启" in t and dx.load()["factory"]["enabled"] is True
    t2 = say("b8", "FOB price for 500 pcs A3?")
    return ok and "500+ pcs" in t2, t + " || " + t2
scenario("f-050", "老板一句话开启工厂模式", f050, factory=False)

def f051():
    t = boss("关闭工厂模式")
    ok = "已关闭" in t and dx.load()["factory"]["enabled"] is False
    t2 = say("b8", "FOB price for 500 pcs A3?")
    return ok and "Ningbo/Yiwu" in t2, t + " || " + t2
scenario("f-051", "老板一句话关闭工厂模式→回档口规则", f051)

n = len(RESULTS); p = sum(1 for r in RESULTS if r[2]); rate = p / n
print("\n" + "=" * 56)
print(f"  Agent C 工厂门禁: {p}/{n} 通过 = {rate:.1%}  (门槛 95%)  ->  {'✅ 放行' if rate >= 0.95 else '❌ 拦截'}")
print("=" * 56)
json.dump({"passed": p, "total": n, "rate": rate,
           "results": [{"id": a, "title": b, "pass": c, "note": ("" if c else str(dd)[:160])} for a, b, c, dd in RESULTS]},
          open("gate_report_factory.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
sys.exit(0 if rate >= 0.95 else 1)
