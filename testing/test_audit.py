# -*- coding: utf-8 -*-
"""Agent C 每日审计 + 周报 测试

三层：
  1. 埋雷：每一类缺陷各埋一条进留痕，审计必须逐条抓出来（漏一条即失败）
  2. 零误报：把真实规则引擎跑一遍正常对话，审计不得报任何非通知类缺陷
  3. 链路：pending/act/notify 事件进留痕、结果标签、周报聚合、/audit 端点、/status 暴露
"""
import os, sys, shutil, json, tempfile, re
TMP = tempfile.mkdtemp(prefix="dxl_audit_")
os.environ.update(WECOM_CORP_ID="ww", WECOM_KF_SECRET="s", WECOM_TOKEN="t",
                  WECOM_AES_KEY="A"*43, BOSS_KEY="k", LLM_ENABLED="0",
                  AUDIT_SCHEDULER="0", DATA_DIR=TMP)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import conv_log as cl
import agent_c_audit as ac
import dianxiaoli_core as dx
from wecom_core import InboundMessage
from fastapi.testclient import TestClient
import wecom_service

P = F = 0
def chk(n, cond, e=""):
    global P, F
    P, F = (P+1, F) if cond else (P, F+1)
    print(("PASS  " if cond else "FAIL  ") + n + ("" if cond else "   <- " + str(e)[:160]))

def m(u, t="", mt="text", media=None, kf="kfA"):
    return InboundMessage(channel="w", msg_id="x", conversation_id="c", sender_id=u,
                          account_id=kf, msg_type=mt, text=t, media_bytes=media, media_id="", raw={})

DAY = "2030-01-15"      # 用未来日期，和真实留痕隔离
def ev(**kw):
    kw.setdefault("ts", f"{DAY}T10:00:00")
    cl._append(kw)

d = dx.load()
d["regulars"]["u_lao"] = "老张"; dx.save(d)   # 供「他人价格泄露」检查用

# ═══════════ 1. 埋雷：每类缺陷一条 ═══════════
planted = {
    "stock_number_leak":    ("A3还有多少",        "A3保温壶 还有 140 件，要多少？"),
    "price_off_catalog":    ("A3什么价",           "A3保温壶 零售 ¥58/件，50件起拿货价 ¥32"),
    "floor_or_cost_leak":   ("底价多少",           "跟您说实话，这款底价是 ¥25，再低没有了"),
    "other_customer_price": ("老张拿多少钱",       "老张那边拿的是 ¥28/件，您也可以"),
    "ai_confirmed_shipping": ("[图片]",            "收到截图，已发货了，明天到"),
    "commitment_missing":   ("要投诉",            "这个我也没办法呀。"),
    "language_mismatch":    ("Hi, do you have A3 hoodies? Price for 50pcs?", "您好，A3保温壶零售45元，拿货价32元。"),
}
i = 0
for cid, (q, a) in planted.items():
    uid = f"trap_{i}"; i += 1
    sc = "图片" if q == "[图片]" else cl.classify_scenario(q)
    ev(ev="in", uid=uid, kf="kfA", type="image" if q == "[图片]" else "text", text=q, scenario=sc, boss=False)
    ev(ev="out", uid=uid, kf="kfA", text=a, scenario=sc, route="rule", boss=False)
# 未回复：顾客说了话，AI 没有回
ev(ev="in", uid="trap_noreply", kf="kfA", type="text", text="B1多少钱", scenario="首次询盘", boss=False)
ev(ev="out", uid="trap_noreply", kf="kfA", text="", scenario="首次询盘", route="rule", boss=False)
# 通知失败
ev(ev="notify", uid="trap_notify", pid=99, ok=False, why="errcode=60020", text="老板确认了")

r = ac.audit_day(DAY, dx)
found = set(r["defects_by_check"])
for cid in list(planted) + ["no_reply", "notify_failed"]:
    chk(f"埋雷被抓: {cid}", cid in found, r["defects_by_check"])
# 「老张那边拿的是 ¥28/件」同时命中 他人价格 + 目录外单价 → 9 条埋雷产出 10 条缺陷，是对的
chk("缺陷总数(含一条双命中)", r["defects_total"] == 10, r["defects_total"])
chk("高危缺陷计数正确", r["defects_high"] == 6, r["defects_high"])
chk("每条缺陷带 uid/ts/摘录", all(x.get("uid") and x.get("ts") and x.get("name") for x in r["defects"]))
chk("报告落盘", ac.load_report("audit", DAY) is not None)
chk("日报文案含缺陷行", "库存数字泄露" in ac.daily_text(r) and "缺陷 10" in ac.daily_text(r), ac.daily_text(r))

# ═══════════ 2. 零误报：真实规则引擎跑正常对话 ═══════════
DAY2 = "2030-01-16"
real_ts = cl.now_iso
cl.now_iso = lambda: f"{DAY2}T11:00:00"
try:
    dx.brain(m("buyer1", "A3什么价"))
    dx.brain(m("buyer1", "来60个"))
    dx.brain(m("buyer1", "能便宜点吗"))
    dx.brain(m("buyer2", "G6帆布袋有货吗"))              # 断货 → 必须含登记/通知
    dx.brain(m("buyer3", "还有多少A3"))                   # 库存红线 → 不报数字
    dx.brain(m("buyer4", "Hi, price for 50 pcs A3?"))     # 英文 → 英文答
    dx.brain(m("buyer5", "质量太差了要投诉"))              # 投诉 → 抱歉+转老板
    dx.brain(m("buyer6", "找老板"))                       # 转人工
    dx.brain(m("buyer7", "", "image", b"\xff\xd8xx"))     # 付款截图 → 不能擅自确认发货
    dx.brain(m("buyer8", "B1来200个，H2来100个"))          # 混合下单
    dx.brain(m("buyer9", "早上好"))                       # 闲聊
finally:
    cl.now_iso = real_ts
r2 = ac.audit_day(DAY2, dx)
non_notify = [x for x in r2["defects"] if x["check"] != "notify_failed"]
chk("真实规则引擎零误报", not non_notify, [(x["check"], x["excerpt"]) for x in non_notify])
chk("会话数统计", r2["sessions"] == 9, r2["sessions"])
chk("场景分类落地", r2["scenarios"].get("议价", 0) >= 1 and r2["scenarios"].get("投诉", 0) >= 1, r2["scenarios"])
chk("结果标签: 下单进待核准", r2["outcomes"].get("待核准", 0) >= 2, r2["outcomes"])
chk("结果标签: 转人工", r2["outcomes"].get("转人工", 0) >= 1, r2["outcomes"])
chk("结果标签: 闲聊", r2["outcomes"].get("闲聊", 0) >= 1, r2["outcomes"])
chk("回复通过率=1", r2["reply_pass_rate"] == 1.0, r2["reply_pass_rate"])

# ═══════════ 3. 链路：核准事件 + 通知进留痕，成交标签 ═══════════
SENT = []
dx.set_notifier(lambda kf, u, t: SENT.append((kf, u, t)))
c = TestClient(wecom_service.app)
DAY3 = "2030-01-17"
cl.now_iso = lambda: f"{DAY3}T12:00:00"
try:
    dx.brain(m("winner", "A3来60个"))
    pid = [p for p in dx.load()["pending"] if p["userid"] == "winner"][-1]["id"]
    c.post("/boss/act?key=k", json={"id": pid, "op": "approve"})
finally:
    cl.now_iso = real_ts
evs3 = cl.read_day(DAY3)
chk("pending 事件进留痕", any(e["ev"] == "pending" and e["uid"] == "winner" for e in evs3))
chk("act 事件进留痕", any(e["ev"] == "act" and e["op"] == "approve" and e["uid"] == "winner" for e in evs3))
chk("notify 事件进留痕(ok)", any(e["ev"] == "notify" and e["ok"] and e["uid"] == "winner" for e in evs3))
r3 = ac.audit_day(DAY3, dx)
chk("核准后标签=成交", r3["outcomes"].get("成交") == 1, r3["outcomes"])
chk("通知计数", r3["notify"]["sent"] >= 1 and r3["notify"]["failed"] == 0, r3["notify"])

# 老板消息不进顾客审计
DAY4 = "2030-01-18"
cl.now_iso = lambda: f"{DAY4}T12:00:00"
d = dx.load(); d["boss_userid"] = "bossman"; dx.save(d)
try:
    dx.brain(m("bossman", "今天卖得怎么样"))
    dx.brain(m("bossman", "待办"))
finally:
    cl.now_iso = real_ts
r4 = ac.audit_day(DAY4, dx)
chk("老板对话不计入顾客会话", r4["sessions"] == 0, r4["sessions"])
chk("老板事件仍留痕(boss=True)", any(e.get("boss") for e in cl.read_day(DAY4)))

# ═══════════ 4. 周报聚合 + 建议 ═══════════
w = ac.audit_week(DAY4, dx)
chk("周报覆盖 7 天", len(w["days"]) == 7 and w["days"][-1] == DAY4)
chk("周报缺陷汇总含埋雷", w["defects_by_check"].get("stock_number_leak") == 1, w["defects_by_check"])
chk("周报有成交率", w["close_rate"] is not None, w)
chk("周报给出针对性建议", any("红线" in s for s in w["suggestions"]), w["suggestions"])
chk("周报文案", "质检周报" in ac.weekly_text(w) and "下周建议" in ac.weekly_text(w))
chk("周报趋势字段", isinstance(w["trend"], dict) and "stock_number_leak" in w["trend"])

# ═══════════ 5. 端点 + /status ═══════════
chk("/audit/daily 鉴权", c.get(f"/audit/daily?date={DAY}").status_code == 401)
j = c.get(f"/audit/daily?key=k&date={DAY}").json()
chk("/audit/daily 返回报告+文案", j.get("defects_total") == 10 and "质检日报" in j.get("text", ""), j.get("defects_total"))
jw = c.get(f"/audit/weekly?key=k&end={DAY4}").json()
chk("/audit/weekly 返回", jw.get("week_end") == DAY4 and "text" in jw)
st = c.get("/status").json()
chk("/status 暴露审计状态", "audit" in st and st["audit"].get("last_weekly", {}).get("week_end") == DAY4, st.get("audit"))
rx = c.get("/risk/export?key=k").json()
chk("风控画像引用审计", rx["quality_assurance"]["daily_audit"].get("enabled") is True, rx["quality_assurance"])

# ═══════════ 6. 老板推送 ═══════════
SENT.clear()
d = dx.load(); d["boss_userid"] = "bossman"; dx.save(d)
rr = ac.run_daily(DAY, push=True)
chk("日报推送到老板微信", rr.get("pushed") and SENT and SENT[-1][1] == "bossman" and "质检日报" in SENT[-1][2], SENT[-1:] )

# ═══════════ 7. 场景分类稳定性 ═══════════
cases = {"A3什么价": "首次询盘", "来60个": "下单", "能便宜点吗": "议价", "找老板": "转人工",
         "质量太差要投诉": "投诉", "我付了": "付款", "还有多少": "问库存",
         "Hi, price for 50 pcs?": "外贸", "早上好": "闲聊", "上新A3 100个": "老板指令"}
bad = {q: cl.classify_scenario(q) for q, want in cases.items() if cl.classify_scenario(q) != want}
chk("场景分类符合预期", not bad, bad)

# ═══════════ 8. 变异测试：把核心的库存保密改坏，审计必须在真实引擎流量上抓到 ═══════════
DAY5 = "2030-01-19"
orig_tail = dx._fmt_stock_tail
dx._fmt_stock_tail = lambda stock: f"还有{stock}件" if stock > 0 else "暂时没货"     # 模拟一次错误改动
cl.now_iso = lambda: f"{DAY5}T13:00:00"
try:
    dx.brain(m("mut1", "A3什么价"))
    dx.brain(m("mut2", "H2多少钱"))
finally:
    dx._fmt_stock_tail = orig_tail
    cl.now_iso = real_ts
r5 = ac.audit_day(DAY5, dx)
chk("变异: 库存保密被改坏 → 审计抓到", r5["defects_by_check"].get("stock_number_leak", 0) >= 2, r5["defects_by_check"])
chk("变异: 缺陷摘录指向原句", any(re.search(r"还有\d+件", x["excerpt"]) for x in r5["defects"]), [x["excerpt"] for x in r5["defects"]][:2])

# 把付款回复改成擅自确认发货
DAY6 = "2030-01-20"
orig_flow = dx._image_payment_flow
dx._image_payment_flow = lambda d, uid, media_id, ocr_amount=0, kfid="": dx.OutboundReply(text="收到截图，这就发货，明天到。")
cl.now_iso = lambda: f"{DAY6}T13:00:00"
try:
    dx.brain(m("mut3", "", "image", b"\xff\xd8xx"))
finally:
    dx._image_payment_flow = orig_flow
    cl.now_iso = real_ts
r6 = ac.audit_day(DAY6, dx)
chk("变异: AI 擅自确认发货 → 审计抓到", r6["defects_by_check"].get("ai_confirmed_shipping", 0) == 1, r6["defects_by_check"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n结果: {P} passed, {F} failed")
sys.exit(1 if F else 0)
