# -*- coding: utf-8 -*-
"""
boss_notify.py — 老板推送通道（新单、回告失败、质检日报/周报 → 企微群机器人）

老板在企微里拉一个群、加「群机器人」，把 webhook 地址填进环境变量 BOSS_WEBHOOK，
店里一有新单就自动推到群里，不用再自己开网页问「待办」。

没配 BOSS_WEBHOOK 时行为与以前完全一致：回退到微信客服通道直接发给老板本人。
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

PUSH_LOG = []            # 最近 20 条推送留痕（/status 可见）
_WINDOW = deque()        # 最近 60 秒的发送时间戳（企微群机器人限 20 条/分钟）
_backlog = []            # 被限流压下的消息，下次有空位时并成一条发
_inflight = set()        # 正在发的投递线程（flush() 等它们）
_LOCK = threading.Lock() # 上面四个共享状态都归它管

RATE_LIMIT = 18          # 留 2 条余量给积压合并那条
RATE_WINDOW = 60
BACKLOG_LINES = 10       # 合并消息最多列几行

_KIND_TITLE = {
    "订单核准": "🧾 新单待核准",
    "付款核验": "💳 付款截图待核对",
    "金额异常": "⚠️ 金额对不上",
    "转人工": "🙋 买家要找老板",
}


def configured():
    """老板配没配群机器人。"""
    return bool(WEBHOOK)


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
    n = len(_backlog)
    lines = [x.replace("\n", " ")[:60] for x in _backlog[:BACKLOG_LINES]]
    more = f"\n…还有 {n - BACKLOG_LINES} 条，打开控制台看全部" if n > BACKLOG_LINES else ""
    return f"⏳ 积压 {n} 条：\n" + "\n".join(lines) + more


def _deliver(text, kind, core):
    """真发一条：先群机器人，不行退微信客服。只在后台线程里跑，慢多久都不碍买家的事。"""
    if WEBHOOK:
        try:
            ok, why = _via_webhook(text)
        except Exception as e:
            ok, why = False, "群机器人发不出去：" + str(e)[:40]
            _rec_err(core, e)
        if ok:
            _log(kind, True, "webhook", "", text)
            return True
    else:
        why = "没配群机器人"
    try:
        ok2, why2 = _via_kf(text, core)
    except Exception as e:
        _rec_err(core, e)
        _log(kind, False, "none", (why + "；微信客服也没发成") if why else str(e)[:60], text)
        return False
    _log(kind, ok2, "kf" if ok2 else "none", "" if ok2 else (why2 or why), text)
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
        for text, kind in jobs:
            try:
                _deliver(text, kind, core)
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
            if WEBHOOK and not _room():   # 限流是群机器人的规矩；走微信客服时不限
                _backlog.append(text)
                backlogged = True
            else:
                backlogged = False
                if _backlog:
                    merged = _merged_backlog()
                    del _backlog[:]
                    if WEBHOOK:
                        _WINDOW.append(time.time())
                if WEBHOOK:
                    _WINDOW.append(time.time())
        if backlogged:
            _log(kind, False, "none", "一分钟内消息太多，稍后并成一条发", text)
            return False
        jobs = ([(merged, "backlog")] if merged else []) + [(text, kind)]
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


def _why_human(why):
    """把技术错误翻成老板能照着做的一句话。

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
            return f"微信客服没发出去（错误码 {code}）。{_CODE_HINT[code]}"
        if re.fullmatch(r"95\d{3}", code):     # 95xxx 是发消息本身被拒，最常见就是超 48 小时
            return _OVER_48H
        return f"微信客服没发出去（错误码 {code}），把这个号告诉技术就能查"
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
