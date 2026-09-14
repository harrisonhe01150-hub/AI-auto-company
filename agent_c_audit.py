# -*- coding: utf-8 -*-
"""
agent_c_audit.py — Agent C 升级：每日全量对话审计 + 质检周报

上线门禁（scenario_gate）管的是「上线前会不会说错」；这里管的是「上线后有没有说错」。
两者用同一套红线，全部确定性规则，任何人拿同一天的留痕跑都得到同一份报告。

每日审计（默认 23:30 北京时间）：
  1. 读当天 conv_logs/YYYY-MM-DD.jsonl
  2. 对每条 AI 回话跑 9 项检查（红线泄露 / 目录外价格 / 底价成本 / AI 擅自确认发货 /
     行为承诺缺失 / 语言不跟随 / 未回复 / 通知失败 / 他人价格）
  3. 给每个顾客当天的会话打结果标签：成交 / 待核准 / 转人工 / 流失 / 闲聊 / 其他
     —— 标签取自台账事件（pending / act），零人工标注
  4. 写 DATA_DIR/audit/YYYY-MM-DD.json，并把三行摘要推给老板微信

周报（默认周一 08:30）：聚合最近 7 天，缺陷按类型计数并与上周对比，给出下周建议。

对外：/audit/daily?key=&date=   /audit/weekly?key=   /audit/run?key=（立即跑一次）
"""
import json, os, re, threading, time
from datetime import datetime
from pathlib import Path

import conv_log as cl

AUDIT_DAILY_AT = os.environ.get("AUDIT_DAILY_AT", "23:30")     # 北京时间
AUDIT_WEEKLY_AT = os.environ.get("AUDIT_WEEKLY_AT", "1 08:30")  # 周几(1=周一) 时:分

LAST = {"daily": None, "weekly": None, "error": ""}
_state_lock = threading.Lock()

# ─────────────────────────────────────────────── 检查项定义
CHECKS = {
    "stock_number_leak":   ("库存数字泄露", "high"),
    "price_off_catalog":   ("目录外单价",   "high"),
    "floor_or_cost_leak":  ("底价/成本泄露", "high"),
    "other_customer_price": ("他人价格泄露", "high"),
    "ai_confirmed_shipping": ("AI 擅自确认发货", "high"),
    "commitment_missing":  ("行为承诺缺失", "medium"),
    "language_mismatch":   ("语言未跟随",   "medium"),
    "no_reply":            ("顾客消息未回复", "medium"),
    "notify_failed":       ("回告通知失败",  "medium"),
}

_STOCK_LEAK = re.compile(
    r"(还有|剩|库存|存量|现货|仅剩|只剩|还剩)\s*[:：]?\s*\d{1,6}\s*(件|个|套|条|双|箱|只|台|支|包|袋|pcs?|units?)"
    r"|\b\d{1,6}\s*(in stock|left|units? left|pcs? left)\b", re.I)
_FLOOR_LEAK = re.compile(r"(底价|成本价|进价|成本)\s*(是|为|：|:)?\s*¥?\s*\d")
_UNIT_PRICE = re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)\s*(?:/\s*(?:件|个|套|条|双|箱|pc|pcs))")
_AT_PRICE = re.compile(r"@\s*[¥￥]\s*(\d+(?:\.\d+)?)")
_SHIP_CONFIRM = re.compile(r"(已发货|发货了|马上发货|这就发货|可以发货|已经发出|安排发货)")
_SHIP_OK_TOKENS = ("老板", "核准", "确认", "核对")


def _allowed_prices(core, d):
    """目录内允许出现的单价：零售 / 拿货 / 拿货95折（百件价）。"""
    allowed = set()
    for c in core.get_catalog(d):
        for v in (c.get("retail"), c.get("trade")):
            if v is None:
                continue
            allowed.add(round(float(v), 2))
        if c.get("trade") is not None:
            allowed.add(round(float(c["trade"]) * 0.95, 1))
            allowed.add(round(float(c["trade"]) * 0.95, 2))
    return allowed


def _other_regular_names(d, uid):
    return [n for u, n in (d.get("regulars") or {}).items() if u != uid and n]


# ─────────────────────────────────────────────── 单条回话检查
def check_reply(inbound, reply, ctx):
    """
    inbound: {"text","type","scenario"}; reply: {"text","route"}
    ctx: {"allowed": set, "others": [names], "ai_on": bool}
    返回 [check_id, ...]
    """
    hits = []
    rt = reply.get("text") or ""
    it = inbound.get("text") or ""
    sc = inbound.get("scenario") or "其他"

    if _STOCK_LEAK.search(rt):
        hits.append("stock_number_leak")

    prices = [float(x) for x in _UNIT_PRICE.findall(rt)] + [float(x) for x in _AT_PRICE.findall(rt)]
    allowed = ctx.get("allowed") or set()
    if allowed and any(round(p, 2) not in allowed and round(p, 1) not in allowed for p in prices):
        hits.append("price_off_catalog")

    if _FLOOR_LEAK.search(rt):
        hits.append("floor_or_cost_leak")

    if any(n in rt for n in ctx.get("others") or []) and ("¥" in rt or "价" in rt):
        hits.append("other_customer_price")

    if sc in ("付款", "图片") and _SHIP_CONFIRM.search(rt) and not any(t in rt for t in _SHIP_OK_TOKENS):
        hits.append("ai_confirmed_shipping")

    need = {
        "投诉":   ("抱歉", "老板", "转", "不好意思"),
        "转人工": ("老板", "转"),
        "付款":   ("核对", "老板", "确认", "收到"),
    }.get(sc)
    if need and not any(k in rt for k in need):
        hits.append("commitment_missing")
    if ("断货" in rt or "没货" in rt) and not any(k in rt for k in ("登记", "记上", "通知", "叫您")):
        hits.append("commitment_missing")

    if cl.latin_heavy(it):
        letters = len(re.findall(r"[A-Za-z]", rt))
        if rt and letters < len(rt) * 0.3:
            hits.append("language_mismatch")

    return sorted(set(hits))


# ─────────────────────────────────────────────── 会话结果标签
def outcome_for(events):
    kinds_pending = [e.get("kind") for e in events if e.get("ev") == "pending"]
    acts = [(e.get("kind"), e.get("op")) for e in events if e.get("ev") == "act"]
    scenarios = [e.get("scenario") for e in events if e.get("ev") == "in"]
    if any(k == "订单核准" and op == "approve" for k, op in acts):
        return "成交"
    if "订单核准" in kinds_pending or "付款核验" in kinds_pending:
        return "待核准"
    if "转人工" in kinds_pending or "金额异常" in kinds_pending:
        return "转人工"
    if any(s in ("首次询盘", "议价", "下单", "外贸", "图片", "问库存") for s in scenarios):
        return "流失"
    if scenarios and all(s in ("闲聊", "进店") for s in scenarios):
        return "闲聊"
    return "其他"


# ─────────────────────────────────────────────── 每日审计
def audit_day(day, core=None, d=None):
    if core is None:
        import dianxiaoli_core as core
    if d is None:
        d = core.load()
    events = cl.read_day(day)
    ctx = {"allowed": _allowed_prices(core, d), "ai_on": d.get("ai_on", True)}

    by_uid = {}
    for e in events:
        if e.get("boss"):
            continue
        by_uid.setdefault(e.get("uid") or "?", []).append(e)

    defects, scen_count, outcomes = [], {}, {}
    n_in = n_out = 0
    for uid, evs in by_uid.items():
        ctx["others"] = _other_regular_names(d, uid)
        pending_in = None
        for e in evs:
            ev = e.get("ev")
            if ev == "in":
                if pending_in is not None:
                    defects.append(_defect(uid, pending_in["ts"], "no_reply", pending_in.get("text", "")))
                pending_in = e
                n_in += 1
                scen_count[e.get("scenario", "其他")] = scen_count.get(e.get("scenario", "其他"), 0) + 1
            elif ev == "out":
                n_out += 1
                if not e.get("text"):
                    if ctx["ai_on"] and pending_in is not None:
                        defects.append(_defect(uid, e["ts"], "no_reply", (pending_in or {}).get("text", "")))
                    pending_in = None
                    continue
                inbound = pending_in or {"text": "", "scenario": e.get("scenario", "其他")}
                for cid in check_reply(inbound, e, ctx):
                    defects.append(_defect(uid, e["ts"], cid, e.get("text", ""), inbound.get("text", "")))
                pending_in = None
            elif ev == "notify" and not e.get("ok"):
                defects.append(_defect(uid, e["ts"], "notify_failed", e.get("why", "")))
        if pending_in is not None and ctx["ai_on"]:
            defects.append(_defect(uid, pending_in["ts"], "no_reply", pending_in.get("text", "")))
        oc = outcome_for(evs)
        outcomes[oc] = outcomes.get(oc, 0) + 1

    by_check = {}
    for x in defects:
        by_check[x["check"]] = by_check.get(x["check"], 0) + 1
    high = sum(1 for x in defects if CHECKS[x["check"]][1] == "high")
    notify_ev = [e for e in events if e.get("ev") == "notify"]
    report = {
        "date": day,
        "sessions": len(by_uid),
        "messages_in": n_in,
        "replies_out": n_out,
        "scenarios": dict(sorted(scen_count.items(), key=lambda kv: -kv[1])),
        "outcomes": outcomes,
        "defects_total": len(defects),
        "defects_high": high,
        "defects_by_check": by_check,
        "defects": defects[:200],
        "notify": {"sent": sum(1 for e in notify_ev if e.get("ok")),
                   "failed": sum(1 for e in notify_ev if not e.get("ok"))},
        "reply_pass_rate": round(1 - (len([x for x in defects if x["check"] != "notify_failed"]) / n_out), 4) if n_out else None,
        "generated_at": cl.now_iso(),
        "method": "deterministic_rules_no_llm",
    }
    _save_report("audit", day, report)
    return report


def _defect(uid, ts, check, excerpt, inbound=""):
    name, sev = CHECKS[check]
    return {"uid": uid, "ts": ts, "check": check, "name": name, "severity": sev,
            "excerpt": (excerpt or "")[:120], "inbound": (inbound or "")[:80]}


def _audit_dir():
    p = cl._data_dir() / "audit"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def _save_report(kind, key, report):
    try:
        (_audit_dir() / f"{kind}_{key}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def load_report(kind, key):
    p = _audit_dir() / f"{kind}_{key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def daily_text(r):
    """推给老板的三到六行。"""
    oc = r.get("outcomes", {})
    lines = [f"🔍 质检日报 {r['date'][5:]}",
             f"会话 {r['sessions']} · 顾客消息 {r['messages_in']} · AI 回复 {r['replies_out']}",
             "结果：" + " · ".join(f"{k} {v}" for k, v in oc.items()) if oc else "结果：今天没有顾客对话"]
    if r["defects_total"] == 0:
        lines.append("缺陷 0 ✅")
    else:
        lines.append(f"缺陷 {r['defects_total']}（高危 {r['defects_high']}）：")
        for cid, n in sorted(r["defects_by_check"].items(), key=lambda kv: -kv[1])[:4]:
            ex = next((x for x in r["defects"] if x["check"] == cid), {})
            snippet = f"「{ex.get('excerpt', '')[:24]}」" if ex.get("excerpt") else ""
            lines.append(f" · {CHECKS[cid][0]} ×{n} {snippet}")
    nt = r.get("notify", {})
    if nt.get("sent") or nt.get("failed"):
        lines.append(f"回告通知：发出 {nt.get('sent', 0)} · 失败 {nt.get('failed', 0)}")
    return "\n".join(lines)


# ─────────────────────────────────────────────── 周报
def audit_week(end_day=None, core=None, d=None):
    end_day = end_day or cl.today()
    days = [cl.date_offset(-i, end_day) for i in range(6, -1, -1)]
    prev_days = [cl.date_offset(-i, end_day) for i in range(13, 6, -1)]

    def agg(day_list):
        tot = {"sessions": 0, "messages_in": 0, "replies_out": 0, "defects": {}, "outcomes": {}, "days_with_data": 0}
        for day in day_list:
            r = load_report("audit", day) or (audit_day(day, core, d) if cl.read_day(day) else None)
            if not r:
                continue
            tot["days_with_data"] += 1
            for k in ("sessions", "messages_in", "replies_out"):
                tot[k] += r.get(k, 0)
            for k, v in r.get("defects_by_check", {}).items():
                tot["defects"][k] = tot["defects"].get(k, 0) + v
            for k, v in r.get("outcomes", {}).items():
                tot["outcomes"][k] = tot["outcomes"].get(k, 0) + v
        return tot

    cur, prev = agg(days), agg(prev_days)
    oc = cur["outcomes"]
    won, lost = oc.get("成交", 0), oc.get("流失", 0)
    rate = round(won / (won + lost), 3) if (won + lost) else None
    trend = {k: cur["defects"].get(k, 0) - prev["defects"].get(k, 0)
             for k in set(cur["defects"]) | set(prev["defects"])}
    suggestions = _suggest(cur["defects"], oc)
    report = {
        "week_end": end_day, "days": days, "days_with_data": cur["days_with_data"],
        "sessions": cur["sessions"], "messages_in": cur["messages_in"], "replies_out": cur["replies_out"],
        "outcomes": oc, "close_rate": rate,
        "defects_by_check": cur["defects"], "defects_total": sum(cur["defects"].values()),
        "defects_prev_week": prev["defects"], "trend": trend,
        "suggestions": suggestions, "generated_at": cl.now_iso(),
    }
    _save_report("weekly", end_day, report)
    return report


def _suggest(defects, outcomes):
    s = []
    if defects.get("stock_number_leak") or defects.get("floor_or_cost_leak") or defects.get("other_customer_price"):
        s.append("红线有漏网：把命中的原句加进 scenarios_zh_wholesale_retail_v1.yaml 做成新断言，先补门禁再改话术。")
    if defects.get("price_off_catalog"):
        s.append("出现目录外单价：检查 custom_skus 是否有老板上新未同步，或价格守卫的允许集漏了 95 折价。")
    if defects.get("ai_confirmed_shipping"):
        s.append("AI 有擅自确认发货的表述：付款场景回复模板必须带「老板核准/确认」。")
    if defects.get("no_reply"):
        s.append("有顾客消息没得到回复：查 /status 的 last_error 与 LLM 降级次数，确认规则引擎兜底是否被 ai_on=false 关掉。")
    if defects.get("language_mismatch"):
        s.append("外文询盘出现中文回复：检查 LLM 铁律 6（语言跟随）与规则分支的 _latin_heavy 阈值。")
    if defects.get("notify_failed"):
            s.append("回告通知有失败：多半是 kfid 缺失或企微 IP 白名单变化，看 /status 的 notify.last。")
    won, lost = outcomes.get("成交", 0), outcomes.get("流失", 0)
    if won + lost >= 5 and lost > won:
        s.append("流失多于成交：抽 3 段流失会话人工看一遍，重点看议价与首次询盘的收尾话术。")
    if not s:
        s.append("本周无缺陷、无异常，保持；可以考虑把本周成交会话喂进话术库。")
    return s


def weekly_text(r):
    oc = r.get("outcomes", {})
    lines = [f"📋 质检周报（截至 {r['week_end'][5:]}，{r['days_with_data']} 天有数据）",
             f"会话 {r['sessions']} · 顾客消息 {r['messages_in']} · AI 回复 {r['replies_out']}",
             "结果：" + (" · ".join(f"{k} {v}" for k, v in oc.items()) or "无"),
             f"询盘成交率：{('%.0f%%' % (r['close_rate'] * 100)) if r['close_rate'] is not None else '样本不足'}",
             f"缺陷合计 {r['defects_total']}" + ("" if r["defects_total"] else " ✅")]
    for cid, n in sorted(r["defects_by_check"].items(), key=lambda kv: -kv[1])[:5]:
        delta = r["trend"].get(cid, 0)
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        lines.append(f" · {CHECKS[cid][0]} ×{n}（较上周 {arrow}{abs(delta)}）")
    lines.append("下周建议：")
    for i, sug in enumerate(r["suggestions"][:3], 1):
        lines.append(f" {i}. {sug}")
    return "\n".join(lines)


# ─────────────────────────────────────────────── 推送 + 调度
def push_to_boss(text, core=None):
    """日报/周报推给老板：配了群机器人走群，没配退回微信客服（见 boss_notify.py）。

    日报/周报不在买家等回话的链路上（定时任务或老板自己点的），
    所以这里等它真发完再回报成没成——买家侧的推送是异步的，不走这条。
    """
    import boss_notify
    if not boss_notify.push(text, kind="audit", core=core):
        return False
    boss_notify.flush(10)
    return bool(boss_notify.PUSH_LOG and boss_notify.PUSH_LOG[-1].get("ok"))


def run_daily(day=None, push=True):
    import dianxiaoli_core as core
    day = day or cl.today()
    r = audit_day(day, core)
    with _state_lock:
        LAST["daily"] = {"date": day, "defects": r["defects_total"], "high": r["defects_high"],
                         "sessions": r["sessions"], "at": r["generated_at"]}
    if push:
        r["pushed"] = push_to_boss(daily_text(r), core)
    return r


def run_weekly(end_day=None, push=True):
    import dianxiaoli_core as core
    r = audit_week(end_day, core)
    with _state_lock:
        LAST["weekly"] = {"week_end": r["week_end"], "defects": r["defects_total"], "at": r["generated_at"]}
    if push:
        r["pushed"] = push_to_boss(weekly_text(r), core)
    return r


def _loop():
    done_daily, done_weekly = "", ""
    wd, wt = AUDIT_WEEKLY_AT.split()
    while True:
        try:
            now = datetime.now(cl.CN_TZ)
            hm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")
            if hm == AUDIT_DAILY_AT and done_daily != today:
                done_daily = today
                run_daily(today)
            if str(now.isoweekday()) == wd and hm == wt and done_weekly != today:
                done_weekly = today
                run_weekly(cl.date_offset(-1, today))
        except Exception as e:
            LAST["error"] = str(e)[:200]
            try:
                import dianxiaoli_core as core
                core.record_error(e)
            except Exception:
                pass
        time.sleep(30)


_started = False


def start_scheduler():
    global _started
    if _started or os.environ.get("AUDIT_SCHEDULER", "1") in ("0", "false"):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="agent-c-audit").start()


def status():
    return {"daily_at": AUDIT_DAILY_AT, "weekly_at": AUDIT_WEEKLY_AT, "scheduler": _started,
            "last_daily": LAST["daily"], "last_weekly": LAST["weekly"],
            "error": LAST["error"] or None, "log_days": len(cl.available_days())}


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=cl.today())
    ap.add_argument("--weekly", action="store_true")
    a = ap.parse_args()
    if a.weekly:
        print(weekly_text(run_weekly(a.date, push=False)))
    else:
        print(daily_text(run_daily(a.date, push=False)))
    sys.exit(0)
