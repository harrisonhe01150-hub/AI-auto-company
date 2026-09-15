# -*- coding: utf-8 -*-
"""凭证按订单归档 + 跟单催付（路线图 v5.1 · A1）

两个洞：
  一、老板事后问「#12 那单的付款截图呢」——pending 被删掉就查不回来了。
      现在凭证进 d["proofs"] 台账，只增不删，按订单号挂。
  二、买家下了单不付款没人跟——followup.check() 到点替老板问一句，只问一次。

红线：催付文案不判断钱到没到、不逼单；老板端回话里绝不出现真实 BOSS_KEY。
"""
import os, sys, tempfile
from datetime import datetime, timedelta

KEY = "ZXQ-PROOFKEY-9911"          # 特征串：断言它不出现在任何对话回复里

os.environ.update(WECOM_CORP_ID="ww", WECOM_KF_SECRET="s", WECOM_TOKEN="t",
                  WECOM_AES_KEY="A" * 43, BOSS_KEY=KEY, LLM_ENABLED="0",
                  AUDIT_SCHEDULER="0", WECOM_COLD_START_SKIP="1",
                  DATA_DIR=tempfile.mkdtemp(prefix="dxl_pf_"), BOSS_WEBHOOK="",
                  FOLLOWUP_ENABLED="1", FOLLOWUP_AFTER_MIN="120")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import boss_notify as bn
import dianxiaoli_core as dx
import followup as fu
from wecom_core import InboundMessage
from fastapi.testclient import TestClient
import wecom_service

P = F = 0
def chk(n, cond, e=""):
    global P, F
    P, F = (P + 1, F) if cond else (P, F + 1)
    print(("PASS  " if cond else "FAIL  ") + n + ("" if cond else "   <- " + str(e)[:160]))


# ── 替身：老板推送 + 回告买家 ────────────────────────────
PUSHES, KF = [], []
bn.push = lambda text, kind="info", core=None: (PUSHES.append({"text": text, "kind": kind}), True)[1]
dx.set_notifier(lambda kf, u, t: KF.append((kf, u, t)))

c = TestClient(wecom_service.app)


def m(u, t="", mt="text", kf="kfA"):
    return InboundMessage(channel="w", msg_id="x", conversation_id="cv", sender_id=u,
                          account_id=kf, msg_type=mt, text=t, media_bytes=None, media_id="", raw={})


def order_ids(uid):
    return [p["id"] for p in dx.load()["pending"]
            if p["userid"] == uid and p["kind"] == "订单核准"]


def proofs():
    return dx.load().get("proofs", [])


def proof_of(pid):
    return next((p for p in proofs() if p["id"] == pid), None)


def ago(hours):
    return (datetime.now(dx.CN_TZ) - timedelta(hours=hours)).strftime("%m-%d %H:%M")


def set_ts(pid, ts):
    d = dx.load()
    for p in d["pending"]:
        if p["id"] == pid:
            p["ts"] = ts
    dx.save(d)


# ══════════════════════════════════════════════════════════
# 一、凭证按订单归档
# ══════════════════════════════════════════════════════════

# 1. 金额一致 → 台账里有这条，挂在订单号下
dx.brain(m("b1", "A3来60个"))
oid1 = order_ids("b1")[-1]
amt1 = next(p["amount"] for p in dx.load()["pending"] if p["id"] == oid1)
d = dx.load(); dx._image_payment_flow(d, "b1", "m_ok.jpg", amt1, "kfA")
pay1 = [p["id"] for p in dx.load()["pending"] if p["kind"] == "付款核验"][-1]
pr = proof_of(pay1)
chk("金额一致 → 凭证进台账", pr is not None, proofs())
chk("金额一致 → order_id 挂对订单", pr and str(pr["order_id"]) == str(oid1), pr)
chk("金额一致 → 状态待核、带图、带金额", pr and pr["status"] == "待核" and pr["media"] == "m_ok.jpg"
    and abs(pr["amount"] - amt1) < 0.01, pr)

# 2. 金额异常 → 同样进台账
dx.brain(m("b2", "A3来60个"))
oid2 = order_ids("b2")[-1]
d = dx.load(); dx._image_payment_flow(d, "b2", "m_bad.jpg", 100, "kfA")
bad2 = [p["id"] for p in dx.load()["pending"] if p["kind"] == "金额异常"][-1]
pr2 = proof_of(bad2)
chk("金额异常 → 凭证进台账", pr2 is not None and pr2["kind"] == "金额异常", proofs())
chk("金额异常 → order_id 挂对订单", pr2 and str(pr2["order_id"]) == str(oid2), pr2)

# 3. 读不出金额：名下恰好一笔待核准订单 → 认这一笔
dx.brain(m("b3", "A3来60个"))
oid3 = order_ids("b3")[-1]
d = dx.load(); dx._image_payment_flow(d, "b3", "m_one.jpg", 0, "kfA")
pay3 = [p["id"] for p in dx.load()["pending"] if p["userid"] == "b3" and p["kind"] == "付款核验"][-1]
chk("只有一笔待核准订单时能自动关联", str(proof_of(pay3)["order_id"]) == str(oid3), proof_of(pay3))

# 4. 两笔待核准订单 → 有歧义就留空，不猜
dx.brain(m("b4", "A3来60个"))
dx.brain(m("b4", "C2来80个"))
chk("b4 名下确实有两笔待核准订单", len(order_ids("b4")) == 2, order_ids("b4"))
d = dx.load(); dx._image_payment_flow(d, "b4", "m_two.jpg", 0, "kfA")
pay4 = [p["id"] for p in dx.load()["pending"] if p["userid"] == "b4" and p["kind"] == "付款核验"][-1]
chk("两笔订单有歧义时 order_id 留空、不瞎猜", proof_of(pay4)["order_id"] == "", proof_of(pay4))

# 5. 核准 / 驳回 → 台账状态跟着改
d = dx.load(); dx._do_act(d, pay1, "approve"); dx.save(d)
chk("核准付款核验 → 台账记「已核准」", proof_of(pay1)["status"] == "已核准", proof_of(pay1))
d = dx.load(); dx._do_act(d, bad2, "reject"); dx.save(d)
chk("驳回金额异常 → 台账记「已驳回」", proof_of(bad2)["status"] == "已驳回", proof_of(bad2))

# 6. pending 删了，台账还在（这就是这个补丁的全部意义）
chk("处理完 pending 里已经没这条了", pay1 not in [p["id"] for p in dx.load()["pending"]],
    dx.load()["pending"])
chk("凭证台账只增不删，事后仍查得到", proof_of(pay1) is not None and proof_of(bad2) is not None, proofs())

# 7. 老板问凭证：两种写法都要答得上
d = dx.load(); d["boss_userid"] = "boss1"; dx.save(d)
t1 = dx.brain(m("boss1", f"{oid1}号凭证")).text
chk("「N号凭证」能查回来", f"#{pay1}" in t1 and "已核准" in t1, t1)
t2 = dx.brain(m("boss1", f"#{oid1}凭证")).text
chk("「#N凭证」也能查回来", f"#{pay1}" in t2 and t2 == t1, t2)
t3 = dx.brain(m("boss1", f"{oid1}号的凭证")).text
chk("「N号的凭证」同样认", t3 == t1, t3)
t4 = dx.brain(m("boss1", f"凭证{oid1}")).text
chk("「凭证N」同样认", t4 == t1, t4)
chk("看图指到控制台，不给点不开的相对路径",
    "图在控制台「付款凭证」那一栏" in t1 and "/boss/media" not in t1, t1)

# 8. 钥匙绝不出现在对话里（老板的 key 不经过对话）
chk("老板端回话里没有真实 BOSS_KEY", KEY not in t1, t1)

# 9. 没读出金额的那条不显示 ¥0
t_amb = dx.brain(m("boss1", f"{oid3}号凭证")).text
chk("金额没读出来时写「金额待核」，不摆一个 ¥0",
    "金额待核" in t_amb and "¥0" not in t_amb, t_amb)

# 10. 只说「凭证」没带单号 → 教他怎么问，别掉进买家兜底
t_bare = dx.brain(m("boss1", "看看凭证")).text
chk("光说「凭证」不掉进买家兜底",
    "说个单号就行" in t_bare and "报个货号" not in t_bare, t_bare)
chk("光说「凭证」时也指了控制台那一栏", "付款凭证" in t_bare and KEY not in t_bare, t_bare)

# 11. 查不到时的文案
t5 = dx.brain(m("boss1", "9988号凭证")).text
chk("查不到时是人话、不是空白", t5 == "单 #9988 还没收到付款凭证。买家发了截图我会马上记上。", t5)
chk("查不到时也不泄露 BOSS_KEY", KEY not in t5, t5)

# 12. /boss/proofs 接口
r = c.get("/boss/proofs?order=1")
chk("/boss/proofs 无 key → 401", r.status_code == 401, r.status_code)
r = c.get(f"/boss/proofs?key={KEY}&order={oid1}")
j = r.json()
chk("带 order 只返回那一单的凭证",
    j["ok"] is True and [x["id"] for x in j["proofs"]] == [pay1], j)
r = c.get(f"/boss/proofs?key={KEY}")
allp = r.json()["proofs"]
chk("不带 order 返回全部凭证", len(allp) == len(proofs()) and len(allp) >= 4, len(allp))

# 13. /boss/state 与老板端页面
s = c.get(f"/boss/state?key={KEY}").json()
chk("/boss/state 带出 proofs 字段", isinstance(s.get("proofs"), list) and len(s["proofs"]) >= 1, s.get("proofs"))
chk("/boss/state 的 proofs 最多 10 条、最新在前", len(s["proofs"]) <= 10
    and s["proofs"][0]["id"] == proofs()[-1]["id"], s["proofs"][:1])
chk("老板端页面有「付款凭证」区", "🧾 付款凭证" in dx.BOSS_HTML and 'id="proofs"' in dx.BOSS_HTML)


# ══════════════════════════════════════════════════════════
# 二、跟单催付
# ══════════════════════════════════════════════════════════
del PUSHES[:]; del KF[:]

# 14. 刚下的单不催
dx.brain(m("f1", "A3来60个"))
f1 = order_ids("f1")[-1]
chk("刚下单不到 2 小时不催", fu.check(core=dx) == [], "不该催")
chk("没催过的单不带 followed_up 标记",
    all(not p.get("followed_up") for p in dx.load()["pending"]), dx.load()["pending"])

# 15. 到点催一次：买家收到一句、老板收到一条、pending 打上标记
set_ts(f1, ago(3))
got = fu.check(core=dx)
chk("超过 2 小时没下文 → 催一次", [x["id"] for x in got] == [f1], got)
chk("买家真的收到了那句话", KF and KF[-1][1] == "f1", KF[-1:])
chk("老板也收到一条跟单提醒", PUSHES and PUSHES[-1]["kind"] == "followup", PUSHES[-1:])
chk("催完给这条 pending 打上 followed_up",
    bool(next(p for p in dx.load()["pending"] if p["id"] == f1).get("followed_up")),
    dx.load()["pending"])

# 16. 只催一次
n_kf, n_push = len(KF), len(PUSHES)
chk("第二次扫不再催同一条", fu.check(core=dx) == [], "重复催了")
chk("第二次扫也不再骚扰买家和老板", len(KF) == n_kf and len(PUSHES) == n_push, (len(KF), len(PUSHES)))

# 17. 文案过红线：不逼单、不提钱到没到、说清是哪笔货
buyer = KF[-1][2]
for bad in ("请尽快付款", "尽快", "到账", "收到钱", "已到账", "催"):
    chk(f"买家文案不含「{bad}」", bad not in buyer, buyer)
chk("买家文案说清是哪笔货", "A3保温壶500ml" in buyer and "×60" in buyer, buyer)
chk("买家文案给了台阶、不逼单", "暂时不方便也说一句" in buyer and "我先给您留着" in buyer, buyer)
chk("生客不会被叫成「顾客您好」", buyer.startswith("您好，您订的") and "顾客您好" not in buyer, buyer)
chk("有真称呼时才连名带姓地问好",
    fu.buyer_text({"name": "张三", "desc": "A3×60"}).startswith("张三您好，"),
    fu.buyer_text({"name": "张三", "desc": "A3×60"}))
boss = PUSHES[-1]["text"]
chk("老板文案带单号", f"#{f1}" in boss and "跟单提醒" in boss, boss)
chk("老板文案说明我已经替他问过了", "我已经替您问了一句" in boss, boss)
chk("3 小时前的单说的是 3 小时", "下单 3 小时没下文" in boss, boss)

# 17b. 时长不许编：不到一小时就按分钟说
chk("35 分钟就说 35 分钟，不许说成 1 小时", fu.span_cn(35) == "35 分钟", fu.span_cn(35))
chk("59 分钟还是分钟", fu.span_cn(59) == "59 分钟", fu.span_cn(59))
chk("满 60 分钟才开始说小时", fu.span_cn(60) == "1 小时" and fu.span_cn(125) == "2 小时",
    (fu.span_cn(60), fu.span_cn(125)))
os.environ["FOLLOWUP_AFTER_MIN"] = "30"
dx.brain(m("f1b", "A3来60个"))
f1b = order_ids("f1b")[-1]
set_ts(f1b, (datetime.now(dx.CN_TZ) - timedelta(minutes=35)).strftime("%m-%d %H:%M"))
chk("间隔设 30 分钟时 35 分钟就催", [x["id"] for x in fu.check(core=dx)] == [f1b], "该催没催")
chk("老板看到的是「35 分钟」而不是「1 小时」",
    "没下文" in PUSHES[-1]["text"] and "分钟没下文" in PUSHES[-1]["text"]
    and "1 小时没下文" not in PUSHES[-1]["text"], PUSHES[-1]["text"])
os.environ["FOLLOWUP_AFTER_MIN"] = "120"

# 17c. 发消息要走网络，中间别人写了盘，不许被旧快照覆盖
dx.brain(m("f1c", "A3来60个"))
f1c = order_ids("f1c")[-1]
set_ts(f1c, ago(3))
_orig_notify = dx._notify
def _notify_with_race(item, text, dd=None):
    other = dx.load()                       # 模拟买家消息这会儿进来了
    other["race_marker"] = "买家这会儿写进来的"
    dx.save(other)
    return _orig_notify(item, text, dd)
dx._notify = _notify_with_race
try:
    fu.check(core=dx)
finally:
    dx._notify = _orig_notify
_after = dx.load()
chk("催付发消息期间别人的写入不被旧快照覆盖", _after.get("race_marker") == "买家这会儿写进来的",
    list(_after.keys()))
chk("同时 followed_up 也确实落了盘",
    bool(next(p for p in _after["pending"] if p["id"] == f1c).get("followed_up")),
    [p for p in _after["pending"] if p["id"] == f1c])

# 18. 已经发过凭证的不催
dx.brain(m("f2", "A3来60个"))
f2 = order_ids("f2")[-1]
set_ts(f2, ago(5))
d = dx.load(); dx._image_payment_flow(d, "f2", "m_f2.jpg", 0, "kfA")
set_ts(f2, ago(5))
chk("名下有付款凭证的单不催", [x["id"] for x in fu.check(core=dx) if x["id"] == f2] == [], "不该催")

# 已核准掉的凭证（pending 里没了，台账里还在）照样算「发过凭证」
d = dx.load()
pay_f2 = [p["id"] for p in d["pending"] if p["userid"] == "f2" and p["kind"] == "付款核验"][-1]
dx._do_act(d, pay_f2, "approve"); dx.save(d)
set_ts(f2, ago(5))
chk("凭证已核准、pending 里没了，台账里有就不催",
    [x["id"] for x in fu.check(core=dx) if x["id"] == f2] == [], "不该催")

# 19. 总开关
dx.brain(m("f3", "A3来60个"))
f3 = order_ids("f3")[-1]
set_ts(f3, ago(4))
os.environ["FOLLOWUP_ENABLED"] = "0"
chk("FOLLOWUP_ENABLED=0 → 一条都不催", fu.check(core=dx) == [], "关了还催")
os.environ["FOLLOWUP_ENABLED"] = "1"
chk("开关打开后照旧能催", [x["id"] for x in fu.check(core=dx)] == [f3], "该催没催")

# 20. 跨月/跨年的时间戳不能被当成「未来」跳过
dx.brain(m("f4", "A3来60个"))
f4 = order_ids("f4")[-1]
set_ts(f4, "12-31 09:00")
jan2 = datetime(datetime.now(dx.CN_TZ).year + 1, 1, 2, 10, 0, tzinfo=dx.CN_TZ)
chk("跨年的 12-31 单子不被误判成未来时间",
    [x["id"] for x in fu.check(core=dx, now=jan2)] == [f4], "跨年被跳过了")

# 21. 时间戳是垃圾串：跳过这条，不抛
dx.brain(m("f5", "A3来60个"))
f5 = order_ids("f5")[-1]
set_ts(f5, "昨天下午")
try:
    got = fu.check(core=dx, now=jan2)
    chk("ts 是垃圾串时不抛异常、只跳过这条", f5 not in [x["id"] for x in got], got)
except Exception as e:
    chk("ts 是垃圾串时不抛异常、只跳过这条", False, e)

# 22. /status 看得见
st = c.get("/status").json()
chk("/status 有 followup 段", isinstance(st.get("followup"), dict), st.get("followup"))
chk("/status 报出开关和间隔",
    st["followup"]["enabled"] is True and st["followup"]["after_min"] == 120, st["followup"])
watched = sum(1 for p in dx.load()["pending"]
              if p["kind"] == "订单核准" and not p.get("followed_up"))
chk("/status 报出当前盯着几单", st["followup"]["pending_watched"] == watched, (st["followup"], watched))

print(f"\n结果: {P} passed, {F} failed")
sys.exit(1 if F else 0)
