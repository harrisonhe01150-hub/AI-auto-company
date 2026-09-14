# -*- coding: utf-8 -*-
"""
boss_notify.py — 老板推送通道（新单、回告失败、质检日报/周报 → 企微群 / 企微应用消息）

两条正式通道，配哪条走哪条，都配就两条都发：
  · 群机器人：老板在企微里拉个群、加「群机器人」，webhook 填进 BOSS_WEBHOOK。
  · 应用消息：企微后台建个自建应用，WECOM_AGENT_ID / WECOM_AGENT_SECRET / BOSS_WECOM_USERID
    三个一填，消息直接进老板的企微聊天框，不用建群、没有 48 小时限制。

两条正式通道都没配、或都发失败时，才回退到微信客服通道直接发给老板本人。
任何推送失败都只记一笔留痕，绝不影响接待主流程。

买家在等回话，推送绝不能拖住他：push() 只做入队和限流判断（几微秒），
真正的网络请求扔给一个 daemon 线程。所以 push() 的返回值是「受理了没有」，
发成没发成看 PUSH_LOG（/status 里的 boss_notify 段）。

给老板看的说明：docs/BOSS_NOTIFY.md
"""
import os, re, threading, time
from collections import deque

import requests

WEBHOOK = os.environ.get("BOSS_WEBHOOK", "").strip()
AGENT_ID = os.environ.get("WECOM_AGENT_ID", "").strip()          # 自建应用 AgentId
AGENT_SECRET = os.environ.get("WECOM_AGENT_SECRET", "").strip()  # 该应用的 Secret
BOSS_USERID = os.environ.get("BOSS_WECOM_USERID", "").strip()    # 老板在通讯录里的账号，多人用 | 分隔

PUSH_LOG = []            # 最近 20 条推送留痕（/status 可见）
_WINDOW = deque()        # 最近 60 秒的发送时间戳（企微群机器人限 20 条/分钟）
_backlog_webhook = []    # 被限流压下的群机器人消息，下次有空位时并成一条发
_backlog = _backlog_webhook   # 老名字，v1.7.3 起的排查脚本/测试照旧能用（同一个列表）
_inflight = set()        # 正在发的投递线程（flush() 等它们）
_LOCK = threading.Lock() # 上面四个共享状态都归它管
_APP_CLIENT = None       # 应用消息的企微客户端（懒加载单例，token 缓存靠它）

RATE_LIMIT = 18          # 留 2 条余量给积压合并那条
RATE_WINDOW = 60
BACKLOG_LINES = 10       # 合并消息最多列几行

_KIND_TITLE = {
    "订单核准": "🧾 新单待核准",
    "付款核验": "💳 付款截图待核对",
    "金额异常": "⚠️ 金额对不上",
    "转人工": "🙋 买家要找老板",
}


def app_configured():
    """应用消息这条通道配齐了没（AgentId + Secret + 老板账号，缺一不算）。"""
    return bool(AGENT_ID and AGENT_SECRET and BOSS_USERID)


def configured():
    """至少有一条正式通道（群机器人 / 应用消息）配好了。"""
    return bool(WEBHOOK) or app_configured()


def channels():
    """已配好的正式通道，按发送顺序。"""
    out = []
    if WEBHOOK:
        out.append("webhook")
    if app_configured():
        out.append("app")
    return out


CHANNEL_CN = {"webhook": "群机器人", "app": "应用消息"}


# ── 留痕 ────────────────────────────────────────────────
def _log(kind, ok, via, why, text):
    with _LOCK:
        PUSH_LOG.append({"ts": time.strftime("%m-%d %H:%M", time.localtime()),
                         "kind": kind, "ok": bool(ok), "via": via,
                         "why": why or "", "text": (text or "")[:60]})
        del PUSH_LOG[:-20]


# ── 两条通道 ────────────────────────────────────────────
def _via_webhook(text):
    """企微群机器人。返回 (ok, 原因)。"""
    r = requests.post(WEBHOOK, json={"msgtype": "markdown",
                                     "markdown": {"content": text}}, timeout=6)
    if getattr(r, "status_code", 0) != 200:
        return False, f"群机器人没收下（HTTP {getattr(r, 'status_code', '?')}）"
    try:
        body = r.json()
    except Exception:
        body = {}
    if body.get("errcode", 0) != 0:
        return False, "群机器人拒收：" + str(body.get("errmsg", ""))[:40]
    return True, ""


def _app_client():
    """应用消息的企微客户端。懒加载单例——同一个实例才有 access_token 缓存和自动重取。"""
    global _APP_CLIENT
    if _APP_CLIENT is None:
        from wecom_core.client import WeComClient
        _APP_CLIENT = WeComClient(corp_id=os.environ["WECOM_CORP_ID"], secret=AGENT_SECRET)
    return _APP_CLIENT


def _via_app(text):
    """企微自建应用消息，直接进老板的企微聊天框。返回 (ok, 原因)。

    markdown 只在企业微信客户端里渲染，老板要是用个人微信互通登录，看到的是纯文本——
    我们的文案本来就是纯文本友好的，不影响。
    """
    from wecom_core.client import WeComAPIError
    payload = {"touser": BOSS_USERID, "msgtype": "markdown",
               "agentid": int(AGENT_ID), "markdown": {"content": text}}
    try:
        r = _app_client()._post("message/send", payload, "app_send") or {}
    except WeComAPIError as e:
        return False, _app_why(e)
    bad = str(r.get("invaliduser") or "").strip()
    if bad:
        return False, f"这些人没收到：{bad}，检查 BOSS_WECOM_USERID 是不是通讯录里的成员账号"
    return True, ""


def _app_why(exc):
    """应用消息的错误码 → 老板照着做的一句话（口径同 _why_human，只是换成应用那套变量）。

    密钥类的三个码单独写一句：_why_human 那句是给微信客服用的，指的变量不对，
    而且拼上失败原因后会超过 60 字被截断，老板看不到该改哪个。
    """
    code = str(getattr(exc, "errcode", "") or "")
    if code in ("40013", "40014", "42001"):
        return f"应用消息没发出去（错误码 {code}），去 Railway 检查 WECOM_AGENT_SECRET"
    return _why_human(str(exc), what="应用消息")


def _via_kf(text, core):
    """回退：走微信客服，直接发给老板本人（逻辑同原 agent_c_audit.push_to_boss）。"""
    if core is None:
        import dianxiaoli_core as core
    d = core.load()
    boss = d.get("boss_userid")
    if not boss or not core.NOTIFIER:
        return False, "老板还没在微信客服里绑定，也没配群机器人"
    kfid = core._kfid_for({"userid": boss}, d)
    if not kfid:
        return False, "找不到跟老板的会话"
    core.NOTIFIER(kfid, boss, text)
    return True, ""


# ── 限流 ────────────────────────────────────────────────
def _room():
    """窗口里还有没有空位（顺手清掉 60 秒前的记录）。调用方必须持有 _LOCK。"""
    now = time.time()
    while _WINDOW and now - _WINDOW[0] > RATE_WINDOW:
        _WINDOW.popleft()
    return len(_WINDOW) < RATE_LIMIT


def _merged_backlog():
    """把压下的几条并成一条。调用方必须持有 _LOCK。"""
    n = len(_backlog_webhook)
    lines = [x.replace("\n", " ")[:60] for x in _backlog_webhook[:BACKLOG_LINES]]
    more = f"\n…还有 {n - BACKLOG_LINES} 条，打开控制台看全部" if n > BACKLOG_LINES else ""
    return f"⏳ 积压 {n} 条：\n" + "\n".join(lines) + more


def _join(reasons):
    """几条失败原因拼成一句，每条最多 60 字。"""
    return "；".join(str(x)[:60] for x in reasons if x)


def _deliver(text, kind, core, want=None):
    """真发一条。只在后台线程里跑，慢多久都不碍买家的事。

    已配好的正式通道**每条都发**，互不影响；全都没发成（或一条都没配）才退回微信客服。
    want=["webhook"] / ["app"] 可以只发其中一条（限流时合并那条只走群机器人）。
    """
    chans = channels()
    if want is not None:
        chans = [c for c in chans if c in want]
    senders = {"webhook": _via_webhook, "app": lambda t: _via_app(t)}
    good, fails = [], []
    for c in chans:
        try:
            ok, why = senders[c](text)
        except Exception as e:
            ok, why = False, f"{CHANNEL_CN[c]}发不出去：" + str(e)[:40]
            _rec_err(core, e)
        if ok:
            good.append(c)
        else:
            fails.append(why or f"{CHANNEL_CN[c]}没发成")
    if good:
        _log(kind, True, "+".join(good), _join(fails), text)
        return True
    try:
        ok2, why2 = _via_kf(text, core)
    except Exception as e:
        _rec_err(core, e)
        _log(kind, False, "none",
             _join(fails + ["微信客服也没发成"]) if fails else str(e)[:60], text)
        return False
    _log(kind, ok2, "kf" if ok2 else "none", "" if ok2 else _join(fails + [why2]), text)
    return ok2


def _rec_err(core, exc):
    try:
        if core is None:
            import dianxiaoli_core as core
        core.record_error(exc)
    except Exception:
        pass


def _run(jobs, core):
    """后台线程的活：按顺序把这几条发出去，然后把自己从在飞名单里摘掉。"""
    try:
        for text, kind, want in jobs:
            try:
                _deliver(text, kind, core, want)
            except Exception as e:
                _rec_err(core, e)
                _log(kind, False, "none", str(e)[:60], text)
    finally:
        with _LOCK:
            _inflight.discard(threading.current_thread())


def push(text, kind="info", core=None):
    """推一条给老板。

    只做入队和限流判断就返回——买家还在等回话，不能让他陪着等企微。
    返回值是「受理了没有」：True = 已交给后台发；False = 空消息，或被限流压下等着并条发。
    真发成没发成看 PUSH_LOG。永不抛异常。
    """
    try:
        if not text:
            return False
        merged = None
        with _LOCK:
            has_app = app_configured()
            if WEBHOOK and not _room():   # 限流只是群机器人的规矩；应用消息和微信客服都不限
                _backlog_webhook.append(text)
                backlogged = True
            else:
                backlogged = False
                if _backlog_webhook:
                    merged = _merged_backlog()
                    del _backlog_webhook[:]
                    if WEBHOOK:
                        _WINDOW.append(time.time())
                if WEBHOOK:
                    _WINDOW.append(time.time())
        if backlogged and not has_app:
            _log(kind, False, "none", "一分钟内消息太多，稍后并成一条发", text)
            return False
        if backlogged:                    # 群里那份攒着并条发，应用消息该发还发
            jobs = [(text, kind, ["app"])]
        else:
            jobs = ([(merged, "backlog", ["webhook"])] if merged else []) + [(text, kind, None)]
        t = threading.Thread(target=_run, args=(jobs, core), daemon=True)
        with _LOCK:
            _inflight.add(t)
        t.start()
        return True
    except Exception as e:
        _rec_err(core, e)
        try:
            _log(kind, False, "none", str(e)[:60], text)
        except Exception:
            pass
        return False


def flush(timeout=5):
    """等所有在飞的推送发完（测试和收尾用）。返回是否等干净了。"""
    end = time.time() + timeout
    while True:
        with _LOCK:
            live = [t for t in _inflight if t.is_alive()]
        if not live:
            return True
        if time.time() >= end:
            return False
        for t in live:
            t.join(max(0.01, end - time.time()))


# ── 人话文案 ────────────────────────────────────────────
def fmt_pending(p):
    """一条待办 → 群里一眼能看懂的话。"""
    p = p or {}
    title = _KIND_TITLE.get(p.get("kind", ""), "📌 待处理")
    pid = p.get("id", "")
    name = p.get("name") or "顾客"
    desc = (p.get("desc") or "").strip()
    if len(desc) > 60:
        desc = desc[:60] + "…"
    try:
        amt = float(p.get("amount") or 0)
    except Exception:
        amt = 0
    money = f" ¥{amt:,.0f}" if amt else ""
    return (f"**{title} #{pid}**\n"
            f"{name} ｜ {desc}{money}\n"
            f"> 回复「核准{pid}号」或打开控制台处理")


# 错误码 → 下一步该干什么（口径同 dianxiaoli_core.record_error 的 hint）
_CODE_HINT = {
    "40013": "微信客服密钥或企业 ID 配错了，去 Railway 检查 WECOM_CORP_ID / WECOM_KF_SECRET",
    "40014": "微信客服密钥或企业 ID 配错了，去 Railway 检查 WECOM_CORP_ID / WECOM_KF_SECRET",
    "42001": "微信客服密钥或企业 ID 配错了，去 Railway 检查 WECOM_CORP_ID / WECOM_KF_SECRET",
    "60020": "出口 IP 变了，打开 /egress 把新 IP 加进企微的可信 IP",
    "95000": "这个客服账号不在企微「通过 API 管理」名单里了，去后台把它加回来",
}
_OVER_48H = "买家超过 48 小时没说话，微信不让主动发"


def _why_human(why, what="微信客服"):
    """把技术错误翻成老板能照着做的一句话（what = 是哪条通道没发出去）。

    注意别用「字符串里有没有 48」来判 48 小时限制——企微的 hint 里带一长串数字，
    `errcode=40013 ... hint: [1789335596486502525157155]` 会被误判成 48 小时，
    把老板往错方向带（真实原因是 corpid/密钥配错）。
    """
    w = str(why or "")
    code = re.search(r"errcode[=: ]*(\d+)", w)
    code = code.group(1) if code else ""
    if re.search(r"48\s*(小时|h|hour)", w, re.I) or "not allowed to send" in w.lower():
        return _OVER_48H
    if code:
        if code in _CODE_HINT:
            return f"{what}没发出去（错误码 {code}）。{_CODE_HINT[code]}"
        if re.fullmatch(r"95\d{3}", code):     # 95xxx 是发消息本身被拒，最常见就是超 48 小时
            return _OVER_48H
        return f"{what}没发出去（错误码 {code}），把这个号告诉技术就能查"
    if "缺少路由" in w or "找不到" in w:
        return "找不到买家的会话"
    if "NOTIFIER" in w or "通道未注入" in w:
        return "微信客服通道还没连上"
    return w[:60] or "没说原因"


def fmt_notify_failed(item, text, why):
    """回告买家失败 → 告诉老板该手动发哪句话。"""
    item = item or {}
    return (f"**📵 没能回告买家**\n"
            f"{item.get('name') or '顾客'}（单 #{item.get('id', '')}）\n"
            f"原因：{_why_human(why)}\n"
            f"请您手动把这句话发给买家：\n"
            f"> {text}")
