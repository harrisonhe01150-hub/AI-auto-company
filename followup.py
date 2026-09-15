# -*- coding: utf-8 -*-
"""
followup.py — 跟单催付提醒（路线图 v5.1 · A1）

买家下了单就没下文，老板也不知道有这么一单悬着。这里只做最便宜的一件事：
下单超过 FOLLOWUP_AFTER_MIN 分钟、名下没有任何付款凭证、且没催过，
就替老板问买家一句，同时给老板推一条「我已经替您问了」。

红线：
  · 不判断钱到没到（只看「有没有收到凭证」这个事实，不判断到账）
  · 不逼单——不说「请尽快付款」，给买家留台阶（「暂时不方便也说一句」）
  · 只催一次，永不自动关单

调度挂在 agent_c_audit 的审计循环里（每 5 分钟一次），不另起线程。
"""
import os
from datetime import datetime, timedelta

DEFAULT_AFTER_MIN = 120


def enabled():
    """FOLLOWUP_ENABLED=0 就整个关掉。"""
    return os.environ.get("FOLLOWUP_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")


def after_min():
    """下单多久没下文才催（分钟）。"""
    try:
        v = int(os.environ.get("FOLLOWUP_AFTER_MIN", str(DEFAULT_AFTER_MIN)))
        return v if v > 0 else DEFAULT_AFTER_MIN
    except Exception:
        return DEFAULT_AFTER_MIN


def _parse_ts(ts, now):
    """台账里的 "%m-%d %H:%M" → datetime。跨年要认出来，认不出就返回 None（跳过这条）。

    只有月日没有年：先按今年算；算出来比现在晚超过 1 天，那就是去年的单（12-31 遇上 01-02）。
    """
    try:
        t = datetime.strptime(str(ts).strip(), "%m-%d %H:%M")
        t = t.replace(year=now.year, tzinfo=now.tzinfo)
        if t - now > timedelta(days=1):
            t = t.replace(year=now.year - 1)
        return t
    except Exception:
        return None


def _has_proof(d, p):
    """这单有没有凭证在路上：名下有付款类待办，或台账里有指向这个单号的记录。"""
    uid = p.get("userid", "")
    if any(x.get("userid") == uid and x.get("kind") in ("付款核验", "金额异常")
           for x in d.get("pending", [])):
        return True
    key = str(p.get("id", ""))
    return any(str(pr.get("order_id", "")) == key for pr in d.get("proofs", []))


def _desc40(p):
    desc = (p.get("desc") or "").strip()
    return desc[:40] + "…" if len(desc) > 40 else desc


def span_cn(mins):
    """多久没下文。不满一小时就按分钟说——不许把 35 分钟说成「1 小时」。"""
    mins = int(mins)
    return f"{mins // 60} 小时" if mins >= 60 else f"{max(1, mins)} 分钟"


def buyer_text(p):
    # 生客在台账里就叫「顾客」，照着念出来太生硬——没有真称呼时直接「您好」
    name = (p.get("name") or "").strip()
    head = f"{name}您好，" if name and name not in ("顾客", "买家") else "您好，"
    return (f"{head}您订的 {_desc40(p)} 我先给您留着。还要的话回我一声，付款截图发我就行；"
            "暂时不方便也说一句，我帮您留到明天。")


def boss_text(p, span):
    name = p.get("name") or "顾客"
    try:
        amt = float(p.get("amount") or 0)
    except Exception:
        amt = 0
    money = f" ¥{amt:,.0f}" if amt else ""
    return (f"**⏰ 跟单提醒 #{p.get('id', '')}**\n"
            f"{name} ｜ {_desc40(p)}{money}\n"
            f"下单 {span}没下文，我已经替您问了一句。")


def check(core=None, now=None):
    """扫一遍待核准订单，该催的催一次。返回被催条目的摘要列表。"""
    if core is None:
        import dianxiaoli_core as core
    if not enabled():
        return []
    now = now or datetime.now(core.CN_TZ)
    limit = after_min()
    d = core.load()
    out = []
    for p in list(d.get("pending", [])):
        if p.get("kind") != "订单核准" or p.get("followed_up"):
            continue
        t = _parse_ts(p.get("ts", ""), now)
        if t is None:
            continue
        mins = (now - t).total_seconds() / 60.0
        if mins < limit:
            continue
        if _has_proof(d, p):
            continue
        # 先把「已催」落盘，再去发消息。发消息要走企微、可能几秒，
        # 这几秒里买家的消息也在 load/save——拿着旧快照回头 save 会把人家的写入整个覆盖掉。
        dd = core.load()
        tgt = next((x for x in dd.get("pending", []) if x.get("id") == p.get("id")), None)
        if not tgt or tgt.get("followed_up"):
            continue
        tgt["followed_up"] = core.now_str()
        core.save(dd)
        span = span_cn(mins)
        notified = core._notify(tgt, buyer_text(tgt), dd)
        try:
            import boss_notify as _bn
            _bn.push(boss_text(tgt, span), kind="followup", core=core)
        except Exception as e:
            core.record_error(e)
        out.append({"id": tgt.get("id"), "userid": tgt.get("userid"), "name": tgt.get("name"),
                    "amount": tgt.get("amount"), "mins": int(mins), "span": span,
                    "notified": bool(notified)})
    return out
