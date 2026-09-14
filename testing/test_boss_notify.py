# -*- coding: utf-8 -*-
"""老板推送（企微群机器人）全链路测试

线上真实漏单：买家下单 → 只写台账，老板收不到任何动静。
这里把「新单推群 / 回告失败推群 / 限流合并 / 没配 webhook 时退回微信客服」四条路全钉住。
"""
import os, sys, tempfile

os.environ.update(WECOM_CORP_ID="ww", WECOM_KF_SECRET="s", WECOM_TOKEN="t",
                  WECOM_AES_KEY="A" * 43, BOSS_KEY="k", LLM_ENABLED="0",
                  AUDIT_SCHEDULER="0", WECOM_COLD_START_SKIP="1",
                  DATA_DIR=tempfile.mkdtemp(prefix="dxl_bn_"), BOSS_WEBHOOK="")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import boss_notify as bn
import dianxiaoli_core as dx
from wecom_core import InboundMessage
from fastapi.testclient import TestClient
import wecom_service                      # 会注入真实通道，下面立刻换成替身

HOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TESTKEY"

P = F = 0
def chk(n, cond, e=""):
    global P, F
    P, F = (P + 1, F) if cond else (P, F + 1)
    print(("PASS  " if cond else "FAIL  ") + n + ("" if cond else "   <- " + str(e)[:160]))


# ── 替身：群机器人 HTTP + 微信客服通道 ──────────────────
class Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = {"errcode": 0, "errmsg": "ok"} if body is None else body
    def json(self):
        return self._body

POSTS, KF = [], []
NEXT = {"resp": Resp(), "boom": None}

def fake_post(url, json=None, timeout=None, **kw):
    POSTS.append({"url": url, "json": json, "timeout": timeout})
    if NEXT["boom"]:
        raise NEXT["boom"]
    return NEXT["resp"]

bn.requests.post = fake_post

def fake_send(kf, u, t):
    if not (kf and u and t):
        raise RuntimeError("缺路由")
    KF.append((kf, u, t))

dx.set_notifier(fake_send)

d = dx.load(); d["boss_userid"] = "boss1"; d["last_kfid"] = "kfA"; dx.save(d)

c = TestClient(wecom_service.app)

def reset(webhook=""):
    bn.flush(5)                         # 上一段的在飞线程别串到这一段来
    bn.WEBHOOK = webhook
    del bn.PUSH_LOG[:]; del bn._backlog[:]; bn._WINDOW.clear()
    del POSTS[:]; del KF[:]
    NEXT["resp"], NEXT["boom"] = Resp(), None
    dx.set_notifier(fake_send)

def sent(text, kind="info"):
    """推一条并等它真发完——线上是异步的，断言之前得等。"""
    ok = bn.push(text, kind=kind, core=dx)
    bn.flush(5)
    return ok

def m(u, t="", mt="text", media=None, kf="kfA"):
    return InboundMessage(channel="w", msg_id="x", conversation_id="c", sender_id=u,
                          account_id=kf, msg_type=mt, text=t, media_bytes=media, media_id="", raw={})


# ── 1. 没配群机器人：完全退回老通道 ─────────────────────
reset("")
chk("没配 webhook 时 configured() 为假", bn.configured() is False)
ok = sent("测试一条")
chk("没配 webhook → 走微信客服发给老板", ok is True and KF and KF[-1][:2] == ("kfA", "boss1"), KF[-1:])
chk("没配 webhook → 一个 HTTP 请求都不发", POSTS == [], POSTS)
chk("留痕记下走的是客服通道", bn.PUSH_LOG[-1]["via"] == "kf" and bn.PUSH_LOG[-1]["ok"], bn.PUSH_LOG[-1:])

reset("")
dx.set_notifier(None)
ok = sent("通道都没有")
chk("通道全没有时不抛，留痕记一笔发不出去", ok is True and bn.PUSH_LOG[-1]["ok"] is False, bn.PUSH_LOG[-1:])
chk("失败原因是人话、没有英文错误码", "老板" in bn.PUSH_LOG[-1]["why"] and "None" not in bn.PUSH_LOG[-1]["why"],
    bn.PUSH_LOG[-1:])

# ── 2. 配了群机器人：POST 的样子 ────────────────────────
reset(HOOK)
chk("配了 webhook 时 configured() 为真", bn.configured() is True)
ok = sent("**新单**\n张三", kind="pending")
chk("发到老板填的 webhook 地址", ok and POSTS and POSTS[-1]["url"] == HOOK, POSTS[-1:])
chk("发的是 markdown 消息", POSTS[-1]["json"]["msgtype"] == "markdown", POSTS[-1]["json"])
chk("正文原样送达", POSTS[-1]["json"]["markdown"]["content"] == "**新单**\n张三", POSTS[-1]["json"])
chk("超时 6 秒，不拖住接待", POSTS[-1]["timeout"] == 6, POSTS[-1]["timeout"])
chk("走群时不再打扰老板私聊", KF == [], KF)

# ── 3. 群机器人发不出去 → 退回微信客服 ──────────────────
reset(HOOK)
NEXT["resp"] = Resp(500, {})
sent("服务器 500")
chk("群机器人 500 → 退回微信客服", KF and "服务器 500" in KF[-1][2], KF[-1:])
chk("留痕记下最终走的是客服", bn.PUSH_LOG[-1]["via"] == "kf", bn.PUSH_LOG[-1:])

reset(HOOK)
NEXT["resp"] = Resp(200, {"errcode": 93000, "errmsg": "invalid webhook url"})
sent("errcode 不为 0")
chk("群机器人拒收 → 退回微信客服", KF and "errcode 不为 0" in KF[-1][2], KF[-1:])

reset(HOOK)
NEXT["boom"] = RuntimeError("connection reset")
sent("网络抽风")
chk("HTTP 抛异常不崩、照样退回客服", KF and "网络抽风" in KF[-1][2], KF[-1:])

reset(HOOK)
NEXT["boom"] = RuntimeError("connection reset")
dx.set_notifier(None)
sent("彻底发不出去")
chk("两条路都断只留一笔失败留痕，不抛", bn.PUSH_LOG[-1]["ok"] is False and bn.PUSH_LOG[-1]["via"] == "none",
    bn.PUSH_LOG[-1:])

# ── 4. 限流：企微群机器人 20 条/分钟 ────────────────────
reset(HOOK)
for i in range(25):
    bn.push(f"第{i + 1}条", kind="pending", core=dx)
bn.flush(5)
chk("一分钟内最多发 18 条", len(POSTS) == 18, len(POSTS))
chk("多出来的 7 条压着没丢", len(bn._backlog) == 7, bn._backlog)
chk("被压下的那几条老板端看得见留痕", bn.PUSH_LOG[-1]["ok"] is False and "太多" in bn.PUSH_LOG[-1]["why"],
    bn.PUSH_LOG[-1:])
bn._WINDOW.clear()                      # 相当于过了一分钟
sent("第26条", kind="pending")
merged = [p["json"]["markdown"]["content"] for p in POSTS if "积压" in p["json"]["markdown"]["content"]]
chk("窗口一有空位就把积压并成一条发出", len(merged) == 1 and "积压 7 条" in merged[0], merged)
chk("合并消息最多列 10 行", merged and merged[0].count("\n") <= 11, merged)
chk("第 26 条本身也发出去了", POSTS[-1]["json"]["markdown"]["content"] == "第26条", POSTS[-1]["json"])

# ── 4b. 推送绝不能拖住买家（企微慢 6 秒，买家不能跟着等） ──
reset(HOOK)
import time as _t
def slow_post(url, json=None, timeout=None, **kw):
    _t.sleep(2)
    POSTS.append({"url": url, "json": json, "timeout": timeout})
    return Resp()
bn.requests.post = slow_post
t0 = _t.time()
dx.brain(m("buyer_fast", "A3来70个"))
cost = _t.time() - t0
chk("企微慢 2 秒，买家的回话仍然立刻出来", cost < 0.5, f"{cost:.2f}s")
bn.flush(5)
chk("慢归慢，最后还是发出去了", len(POSTS) == 1, POSTS)
bn.requests.post = fake_post

# ── 5. 文案：待办四种 kind ──────────────────────────────
def pend(kind, amount=900, desc="P1不锈钢饭盒 ×50", pid=12, name="张三"):
    return {"id": pid, "name": name, "desc": desc, "amount": amount, "kind": kind}

t = bn.fmt_pending(pend("订单核准"))
chk("订单核准文案", t.startswith("**🧾 新单待核准 #12**") and "张三 ｜ P1不锈钢饭盒 ×50 ¥900" in t, t)
chk("文案告诉老板下一步怎么办", "回复「核准12号」" in t, t)
chk("付款核验文案", bn.fmt_pending(pend("付款核验")).startswith("**💳 付款截图待核对 #12**"),
    bn.fmt_pending(pend("付款核验")))
chk("金额异常文案", bn.fmt_pending(pend("金额异常")).startswith("**⚠️ 金额对不上 #12**"),
    bn.fmt_pending(pend("金额异常")))
chk("转人工文案", bn.fmt_pending(pend("转人工", amount=0)).startswith("**🙋 买家要找老板 #12**"),
    bn.fmt_pending(pend("转人工", amount=0)))
chk("没见过的 kind 也有兜底标题", bn.fmt_pending(pend("转工程", amount=0)).startswith("**📌 待处理 #12**"),
    bn.fmt_pending(pend("转工程", amount=0)))
chk("金额为 0 时不显示金额", "¥" not in bn.fmt_pending(pend("转人工", amount=0)).split("\n")[1],
    bn.fmt_pending(pend("转人工", amount=0)))
long_desc = "投诉" * 50
chk("desc 超 60 字截断", len(bn.fmt_pending(pend("转人工", amount=0, desc=long_desc)).split("\n")[1]) < 80,
    bn.fmt_pending(pend("转人工", amount=0, desc=long_desc)))

# ── 6. 文案：回告失败 ───────────────────────────────────
item = {"id": 7, "name": "李姐", "userid": "u9"}
t = bn.fmt_notify_failed(item, "老板确认了！这就发货。", "errcode=95001 not allowed to send message to this user")
chk("回告失败标题", t.startswith("**📵 没能回告买家**") and "李姐（单 #7）" in t, t)
chk("48 小时限制说人话", "买家超过 48 小时没说话" in t and "95001" not in t, t)
chk("把原话给老板让他手动补发", "请您手动把这句话发给买家" in t and "老板确认了" in t, t)
chk("缺路由说人话", "找不到买家的会话" in bn.fmt_notify_failed(item, "x", "缺少路由信息 kfid=无 userid=有"),
    bn.fmt_notify_failed(item, "x", "缺少路由信息 kfid=无 userid=有"))
chk("通道没接上也说人话", "微信客服通道还没连上" in bn.fmt_notify_failed(item, "x", "通道未注入(NOTIFIER=None)"),
    bn.fmt_notify_failed(item, "x", "通道未注入(NOTIFIER=None)"))

# 企微的 hint 里带一长串数字，光看「有没有 48」会把配错密钥误判成 48 小时限制
real_40013 = ("[gettoken] errcode=40013 errmsg=invalid corpid, "
              "hint: [1789335596486502525157155], from ip: 35.1.2.3")
t = bn.fmt_notify_failed(item, "x", real_40013)
chk("corpid 配错不许被说成 48 小时限制", "48 小时" not in t, t)
chk("corpid 配错要报出错误码和该改哪个变量",
    "错误码 40013" in t and "WECOM_CORP_ID" in t, t)
t = bn.fmt_notify_failed(item, "x", "errcode=60020 errmsg=not allow to access from your ip, from ip: 1.2.3.4")
chk("出口 IP 变了要告诉老板开 /egress", "/egress" in t and "错误码 60020" in t, t)

# ── 7. 真实链路：买家下单 → 老板群里立刻看到 ─────────────
reset(HOOK)
dx.brain(m("buyer1", "A3来60个"))
bn.flush(5)
pend_logs = [x for x in bn.PUSH_LOG if x["kind"] == "pending"]
chk("买家下单立刻推给老板", len(pend_logs) == 1 and pend_logs[-1]["ok"], bn.PUSH_LOG)
content = POSTS[-1]["json"]["markdown"]["content"]
chk("推的内容带单号和怎么核准", "#" in content and "核准" in content, content)

# ── 8. 核准后回告买家失败 → 也推给老板 ──────────────────
reset(HOOK)
def boom_send(kf, u, t):
    raise RuntimeError("errcode=95001 not allowed to send message")
dx.set_notifier(boom_send)
pid = [p for p in dx.load()["pending"] if p["userid"] == "buyer1"][0]["id"]
r = c.post("/boss/act?key=k", json={"id": pid, "op": "approve"})
bn.flush(5)
fails = [x for x in bn.PUSH_LOG if x["kind"] == "notify_failed"]
chk("回告失败 → 推给老板", len(fails) >= 1, bn.PUSH_LOG)
chk("推送失败也不影响核准本身", r.json().get("ok") is True, r.json())

# ── 9. /status 看得见 ──────────────────────────────────
reset(HOOK)
sent("给自检看的一条")
st = c.get("/status").json()
chk("自检页有 boss_notify 段", "boss_notify" in st and st["boss_notify"]["configured"] is True, st.get("boss_notify"))
chk("自检页给出成功条数和最近几条", st["boss_notify"]["sent"] >= 1 and st["boss_notify"]["last"],
    st.get("boss_notify"))

# ── 10. 帮助文案两种状态 ────────────────────────────────
reset("")
h = dx.brain(m("boss1", "帮助")).text
chk("没配时帮助里教老板去配", "配好群机器人后新单会自动推到群里" in h and "BOSS_NOTIFY.md" in h, h)
reset(HOOK)
h = dx.brain(m("boss1", "帮助")).text
chk("配好后帮助里说已经在推了", "📣 通知：新单已自动推群" in h, h)

# ── 11. I-07 单字关键词不再误伤 ─────────────────────────
reset(HOOK)
chk("「在线吗」不再被当成问数据线", dx.find_item("在线吗") is None, dx.find_item("在线吗"))
chk("「上线了」也不算问货", dx.find_item("上线了") is None, dx.find_item("上线了"))
chk("「伞多少钱」仍命中 F1 雨伞", (dx.find_item("伞多少钱") or {}).get("sku") == "F1", dx.find_item("伞多少钱"))
chk("长句里的单字关键词照旧命中", (dx.find_item("这条裙子有 M 码吗？多少钱？") or {}).get("sku") == "D1",
    dx.find_item("这条裙子有 M 码吗？多少钱？"))

# ── 12. I-08 幻觉红线：没有相近款时列出在售 ──────────────
t = dx.brain(m("buyer9", "Z9 太阳能板多少钱")).text
chk("不认识的货号照旧不瞎报价", "Z9" in t and "不敢瞎报价" in t, t)
chk("没有相近款时直接列出在售前 3 款", "目前在售：" in t and "A3保温壶500ml、B1玻璃杯、C2吸管保温杯" in t, t)
chk("不再用「说下商品类目」搪塞", "说下商品类目" not in t, t)

# ── 13. 第二条正式通道：企微应用消息（v1.7.4） ───────────
from wecom_core.client import WeComAPIError

APP = []                                  # 应用消息发出去的 payload
APP_NEXT = {"ret": {"errcode": 0, "errmsg": "ok"}, "boom": None}

class FakeClient:
    def _post(self, path, payload, where):
        APP.append({"path": path, "payload": payload, "where": where})
        if APP_NEXT["boom"]:
            raise APP_NEXT["boom"]
        return APP_NEXT["ret"]

bn._app_client = lambda: FakeClient()

def reset_app(webhook="", app=True):
    """清干净现场：webhook 配不配、应用消息配不配，都在这儿定。"""
    reset(webhook)
    bn.AGENT_ID = "1000002" if app else ""
    bn.AGENT_SECRET = "sec" if app else ""
    bn.BOSS_USERID = "bosswx" if app else ""
    del APP[:]
    APP_NEXT["ret"], APP_NEXT["boom"] = {"errcode": 0, "errmsg": "ok"}, None

# 13.1 三个变量缺一不可
reset_app("", app=True)
chk("三个变量齐了 → 应用消息通道算配好", bn.app_configured() is True)
for miss in ("AGENT_ID", "AGENT_SECRET", "BOSS_USERID"):
    reset_app("", app=True); setattr(bn, miss, "")
    chk(f"缺 {miss} 就不算配好", bn.app_configured() is False, miss)

# 13.2 channels() 四种组合
reset_app("", app=False)
chk("两条都没配 → channels 为空、configured 为假", bn.channels() == [] and bn.configured() is False, bn.channels())
reset_app(HOOK, app=False)
chk("只配群机器人 → channels=['webhook']", bn.channels() == ["webhook"], bn.channels())
reset_app("", app=True)
chk("只配应用消息 → channels=['app']、configured 为真",
    bn.channels() == ["app"] and bn.configured() is True, bn.channels())
reset_app(HOOK, app=True)
chk("两条都配 → channels=['webhook','app']", bn.channels() == ["webhook", "app"], bn.channels())

# 13.3 只配应用消息时的 payload
reset_app("", app=True)
ok = sent("**🧾 新单待核准 #21**\n王姐", kind="pending")
chk("只配应用消息 → 调的是 message/send", ok and APP and APP[-1]["path"] == "message/send", APP[-1:])
p = APP[-1]["payload"]
chk("发给老板本人的 userid", p["touser"] == "bosswx", p)
chk("agentid 是整数", p["agentid"] == 1000002 and isinstance(p["agentid"], int), p)
chk("正文原样、markdown 格式", p["msgtype"] == "markdown" and p["markdown"]["content"].endswith("王姐"), p)
chk("只配应用消息时不打群机器人的接口", POSTS == [], POSTS)
chk("留痕 via=app", bn.PUSH_LOG[-1]["via"] == "app" and bn.PUSH_LOG[-1]["ok"], bn.PUSH_LOG[-1:])
chk("走应用消息时不再打扰微信客服", KF == [], KF)

# 13.4 两条都配：两边都发
reset_app(HOOK, app=True)
sent("两边都要收到")
chk("群机器人收到了", len(POSTS) == 1 and POSTS[-1]["json"]["markdown"]["content"] == "两边都要收到", POSTS)
chk("应用消息也收到了", len(APP) == 1 and APP[-1]["payload"]["markdown"]["content"] == "两边都要收到", APP)
chk("留痕 via=webhook+app", bn.PUSH_LOG[-1]["via"] == "webhook+app", bn.PUSH_LOG[-1:])
chk("两条都成时不回退微信客服", KF == [], KF)

# 13.5 一条失败不影响另一条
reset_app(HOOK, app=True)
NEXT["resp"] = Resp(500, {})
sent("群机器人挂了")
chk("群机器人挂了，应用消息照样送到", len(APP) == 1, APP)
chk("只要有一条成了就算成功、via=app", bn.PUSH_LOG[-1]["ok"] is True and bn.PUSH_LOG[-1]["via"] == "app",
    bn.PUSH_LOG[-1:])
chk("留痕里说清群机器人为什么没成", "群机器人" in bn.PUSH_LOG[-1]["why"], bn.PUSH_LOG[-1:])
chk("还有一条成功时不去打扰微信客服", KF == [], KF)

# 13.6 两条都失败 → 回退微信客服
reset_app(HOOK, app=True)
NEXT["boom"] = RuntimeError("connection reset")
APP_NEXT["boom"] = WeComAPIError(60020, "not allow to access from your ip", "app_send")
sent("两条都断了")
chk("两条正式通道都失败 → 回退微信客服", KF and "两条都断了" in KF[-1][2], KF[-1:])
chk("回退成功时留痕记 kf", bn.PUSH_LOG[-1]["via"] == "kf" and bn.PUSH_LOG[-1]["ok"], bn.PUSH_LOG[-1:])

reset_app(HOOK, app=True)
NEXT["boom"] = RuntimeError("connection reset")
APP_NEXT["boom"] = WeComAPIError(60020, "not allow to access from your ip", "app_send")
dx.set_notifier(None)
sent("三条全断")
chk("三条全断 → ok False、via=none", bn.PUSH_LOG[-1]["ok"] is False and bn.PUSH_LOG[-1]["via"] == "none",
    bn.PUSH_LOG[-1:])
chk("两条失败原因都留在 why 里",
    "群机器人" in bn.PUSH_LOG[-1]["why"] and "/egress" in bn.PUSH_LOG[-1]["why"], bn.PUSH_LOG[-1:])

# 13.7 出口 IP / userid 填错都说人话
reset_app("", app=True)
APP_NEXT["boom"] = WeComAPIError(60020, "not allow to access from your ip", "app_send")
dx.set_notifier(None)
sent("IP 变了")
chk("应用消息 60020 → 告诉老板开 /egress", "/egress" in bn.PUSH_LOG[-1]["why"], bn.PUSH_LOG[-1:])
chk("错误提示里没有英文报错原文", "errcode" not in bn.PUSH_LOG[-1]["why"], bn.PUSH_LOG[-1:])

reset_app("", app=True)
APP_NEXT["boom"] = WeComAPIError(40013, "invalid corpid", "app_send")
dx.set_notifier(None)
sent("密钥配错")
chk("应用消息 40013 → 指到 WECOM_AGENT_SECRET",
    "WECOM_AGENT_SECRET" in bn.PUSH_LOG[-1]["why"] and "WECOM_KF_SECRET" not in bn.PUSH_LOG[-1]["why"],
    bn.PUSH_LOG[-1:])

reset_app("", app=True)
APP_NEXT["ret"] = {"errcode": 0, "errmsg": "ok", "invaliduser": "bosswx"}
dx.set_notifier(None)
sent("userid 填错了")
why = bn.PUSH_LOG[-1]["why"]
chk("有人没收到 → 不算成功", bn.PUSH_LOG[-1]["ok"] is False, bn.PUSH_LOG[-1:])
chk("告诉老板是谁没收到、该去哪儿改", "这些人没收到" in why and "bosswx" in why and "BOSS_WECOM_USERID" in why, why)

# 13.8 限流只管群机器人，应用消息照发
reset_app(HOOK, app=True)
for i in range(18):
    bn.push(f"第{i + 1}条", kind="pending", core=dx)
bn.flush(5)
chk("18 条把群机器人的窗口塞满", len(POSTS) == 18 and len(APP) == 18, (len(POSTS), len(APP)))
sent("第19条", kind="pending")
chk("第 19 条群里先攒着", len(POSTS) == 18 and len(bn._backlog_webhook) == 1, (len(POSTS), bn._backlog_webhook))
chk("第 19 条的应用消息照样立刻发出", len(APP) == 19 and APP[-1]["payload"]["markdown"]["content"] == "第19条",
    APP[-1:])
chk("被限流时留痕记 via=app 而不是失败", bn.PUSH_LOG[-1]["via"] == "app" and bn.PUSH_LOG[-1]["ok"],
    bn.PUSH_LOG[-1:])
bn._WINDOW.clear()
sent("第20条", kind="pending")
merged = [x["json"]["markdown"]["content"] for x in POSTS if "积压" in x["json"]["markdown"]["content"]]
chk("窗口空出来后把积压并成一条只发群里", len(merged) == 1 and "积压 1 条" in merged[0], merged)
chk("合并那条不重复发应用消息", len([x for x in APP if "积压" in x["payload"]["markdown"]["content"]]) == 0,
    APP[-2:])

# 13.9 /status 和帮助文案
reset_app(HOOK, app=True)
st = c.get("/status").json()
chk("自检页能看出哪几条通道开着", st["boss_notify"]["channels"] == ["webhook", "app"], st.get("boss_notify"))
reset_app("", app=True)
chk("只开应用消息时自检页也显示 configured",
    c.get("/status").json()["boss_notify"]["channels"] == ["app"], c.get("/status").json().get("boss_notify"))

reset_app(HOOK, app=True)
h = dx.brain(m("boss1", "帮助")).text
chk("两条都开时帮助里两条都点名", "群机器人 + 应用消息" in h, h)
reset_app("", app=True)
h = dx.brain(m("boss1", "帮助")).text
chk("只开应用消息时帮助里说的是应用消息", "应用消息" in h and "群机器人" not in h, h)
reset_app("", app=False)
h = dx.brain(m("boss1", "帮助")).text
chk("一条都没配时仍旧引导老板去配", "配好群机器人后新单会自动推到群里" in h, h)

reset_app("", app=False)                  # 收尾：别把 app 配置留给后面的用例

print(f"\n结果: {P} passed, {F} failed")
sys.exit(1 if F else 0)
