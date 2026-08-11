# -*- coding: utf-8 -*-
"""核准/驳回 → 主动回告顾客 全链路测试

测试替身刻意做成和线上一样严格：kfid/userid 缺一就发不出去。
（第一版替身无脑 append，把"付款核验待办没带 kfid"这个真 bug 盖住了。）
"""
import os, sys, shutil
os.environ.update(WECOM_CORP_ID="ww", WECOM_KF_SECRET="s", WECOM_TOKEN="t",
                  WECOM_AES_KEY="A"*43, BOSS_KEY="k", LLM_ENABLED="0")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
from pathlib import Path
if Path("dianxiaoli_data.json").exists(): Path("dianxiaoli_data.json").unlink()
if Path("dianxiaoli_media").exists(): shutil.rmtree("dianxiaoli_media")

import dianxiaoli_core as dx
from wecom_core import InboundMessage
from fastapi.testclient import TestClient
import wecom_service                      # ← 先导入（它会注入真实通道）

SENT, DROPPED = [], []
def fake_send(kf, u, t):
    """与 wecom_service._notify 同样的前置条件：缺路由信息就发不出去"""
    if not (kf and u and t):
        DROPPED.append((kf, u, t)); return
    SENT.append((kf, u, t))
dx.set_notifier(fake_send)

c = TestClient(wecom_service.app)
P = F = 0
def chk(n, cond, e=""):
    global P, F
    P, F = (P+1, F) if cond else (P, F+1)
    print(("PASS  " if cond else "FAIL  ") + n + ("" if cond else "   <- " + str(e)[:140]))
def m(u, t="", mt="text", media=None, kf="kfA"):
    return InboundMessage(channel="w", msg_id="x", conversation_id="c", sender_id=u,
                          account_id=kf, msg_type=mt, text=t, media_bytes=media, media_id="", raw={})

dx.brain(m("buyer1", "A3来60个"))
d = dx.load(); pid = [p for p in d["pending"] if p["kind"] == "订单核准"][0]["id"]
chk("待办记录kfid", d["pending"][0].get("kfid") == "kfA", d["pending"][0])
c.post("/boss/act?key=k", json={"id": pid, "op": "approve"})
chk("核准→通知顾客", SENT and "老板确认了" in SENT[-1][2], SENT[-1] if SENT else None)
chk("通知发往正确会话", SENT[-1][0] == "kfA" and SENT[-1][1] == "buyer1", SENT[-1])
chk("通知含金额", "1,920" in SENT[-1][2], SENT[-1][2])

dx.brain(m("buyer2", "B1来200个"))
pid2 = [p for p in dx.load()["pending"] if p["userid"] == "buyer2"][0]["id"]
c.post("/boss/act?key=k", json={"id": pid2, "op": "reject"})
chk("驳回→通知顾客", "接不了" in SENT[-1][2], SENT[-1][2])

# ── 付款截图链路：这里正是线上"点核准顾客收不到"的那条路 ──
dx.brain(m("buyer3", "A3来80个")); dx.brain(m("buyer3", "", "image", b"\xff\xd8xx"))
pay_item = [p for p in dx.load()["pending"] if p["kind"] == "付款核验"][0]
chk("付款核验待办带kfid", pay_item.get("kfid") == "kfA", pay_item)
n_drop = len(DROPPED)
c.post("/boss/act?key=k", json={"id": pay_item["id"], "op": "approve"})
chk("付款核验→真的发出去了", len(DROPPED) == n_drop, DROPPED[-1:] )
chk("付款核验通知措辞", "款已收到" in SENT[-1][2], SENT[-1][2])
chk("付款核验发往正确会话", SENT[-1][:2] == ("kfA", "buyer3"), SENT[-1])

# ── 历史遗留待办（老版本写进去时没带 kfid）→ 会话兜底找回 ──
d = dx.load()
legacy = {"id": 9001, "userid": "buyer3", "name": "顾客", "desc": "旧版付款截图",
          "amount": 0, "kind": "付款核验", "media": "", "kfid": "", "ts": dx.now_str()}
d["pending"].append(legacy); dx.save(d)
c.post("/boss/act?key=k", json={"id": 9001, "op": "approve"})
chk("无kfid的旧待办也能回告", SENT[-1][:2] == ("kfA", "buyer3"), SENT[-1])

# ── 全新顾客+全新待办都没 kfid → 退到全局最近客服账号 ──
d = dx.load()
d["sessions"].pop("ghost", None)
d["pending"].append({"id": 9002, "userid": "ghost", "name": "顾客", "desc": "无会话记录",
                     "amount": 0, "kind": "付款核验", "media": "", "kfid": "", "ts": dx.now_str()})
dx.save(d)
c.post("/boss/act?key=k", json={"id": 9002, "op": "approve"})
chk("无会话记录时退到last_kfid", SENT[-1][:2] == ("kfA", "ghost"), SENT[-1])

dx.brain(m("buyer4", "A3来55个"))
before = len(SENT)
c.post("/boss/ask?key=k", json={"text": "全部核准"})
chk("对话式核准也通知", len(SENT) > before)

def boom(kf, u, t): raise RuntimeError("kf api down")
dx.set_notifier(boom)
dx.brain(m("buyer5", "B1来300个"))
pid5 = [p for p in dx.load()["pending"] if p["userid"] == "buyer5"][0]["id"]
r = c.post("/boss/act?key=k", json={"id": pid5, "op": "approve"})
chk("通知失败不阻断核准", r.json().get("ok") and any(o["userid"] == "buyer5" for o in dx.load()["orders"]))
chk("失败原因写进留痕", dx.NOTIFY_LOG[-1].get("ok") is False and "kf api down" in dx.NOTIFY_LOG[-1].get("why", ""),
    dx.NOTIFY_LOG[-1])
chk("通知留痕可查", len(dx.NOTIFY_LOG) >= 5, len(dx.NOTIFY_LOG))

# ── /status 能直接看出通知发没发出去 ──
st = c.get("/status").json()
chk("自检页暴露通知统计", "notify" in st and st["notify"]["sent"] >= 5, st.get("notify"))
chk("自检页给出失败原因", any(x.get("why") for x in st["notify"]["last"] if not x.get("ok")) or st["notify"]["failed"] >= 1,
    st.get("notify"))

print(f"\n结果: {P} passed, {F} failed")
sys.exit(1 if F else 0)
