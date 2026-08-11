# -*- coding: utf-8 -*-
"""核准/驳回 → 主动回告顾客 全链路测试"""
import os, sys, shutil
os.environ.update(WECOM_CORP_ID="ww", WECOM_KF_SECRET="s", WECOM_TOKEN="t",
                  WECOM_AES_KEY="A"*43, BOSS_KEY="k", LLM_ENABLED="0")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if os.path.basename(os.path.dirname(os.path.abspath(__file__))) == "testing" else os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
from pathlib import Path
if Path("dianxiaoli_data.json").exists(): Path("dianxiaoli_data.json").unlink()
if Path("dianxiaoli_media").exists(): shutil.rmtree("dianxiaoli_media")

import dianxiaoli_core as dx
from wecom_core import InboundMessage
from fastapi.testclient import TestClient
import wecom_service                      # ← 先导入（它会注入真实通道）
SENT = []
dx.set_notifier(lambda kf, u, t: SENT.append((kf, u, t)))   # ← 再覆盖为测试通道

c = TestClient(wecom_service.app)
P = F = 0
def chk(n, cond, e=""):
    global P, F
    P, F = (P+1, F) if cond else (P, F+1)
    print(("PASS  " if cond else "FAIL  ") + n + ("" if cond else "   <- " + str(e)[:140]))
def m(u, t="", mt="text", media=None):
    return InboundMessage(channel="w", msg_id="x", conversation_id="c", sender_id=u,
                          account_id="kfA", msg_type=mt, text=t, media_bytes=media, media_id="", raw={})

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

dx.brain(m("buyer3", "A3来80个")); dx.brain(m("buyer3", "", "image", b"\xff\xd8xx"))
pay = [p for p in dx.load()["pending"] if p["kind"] == "付款核验"][0]["id"]
c.post("/boss/act?key=k", json={"id": pay, "op": "approve"})
chk("付款核验通知措辞", "款已收到" in SENT[-1][2], SENT[-1][2])

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
chk("通知留痕可查", len(dx.NOTIFY_LOG) >= 5, len(dx.NOTIFY_LOG))

print(f"\n结果: {P} passed, {F} failed")
sys.exit(1 if F else 0)
