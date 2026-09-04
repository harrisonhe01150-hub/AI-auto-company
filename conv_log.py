# -*- coding: utf-8 -*-
"""
conv_log.py — 对话留痕（Agent C 每日审计的数据源）

设计：
  * 按天写 JSONL：DATA_DIR/conv_logs/YYYY-MM-DD.jsonl，追加即写、永不改写，主数据 JSON 不膨胀。
  * 每条记录一个「事件」：顾客进话(in) / AI 回话(out) / 老板指令(boss) / 待办生成(pending) /
    核准驳回(act) / 主动通知(notify)。审计只读这份文件，不碰线上数据。
  * 场景分类是确定性的关键词规则，和 skill_loader 的 scenario 名对齐，方便日后技能注入复用。
  * 任何写入异常都吞掉并记到 record_error —— 留痕绝不能影响接待。
"""
import json, os, re, threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
_LOCK = threading.Lock()
_ERR_HOOK = None          # core.record_error 注入
MAX_TEXT = 400            # 单条正文截断，够审计用，不存整段长文


def _data_dir():
    return Path(os.environ.get("DATA_DIR", "."))


def log_dir():
    p = _data_dir() / "conv_logs"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def set_error_hook(fn):
    global _ERR_HOOK
    _ERR_HOOK = fn


def now_iso():
    return datetime.now(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def today():
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def date_offset(days, base=None):
    b = datetime.strptime(base, "%Y-%m-%d") if base else datetime.now(CN_TZ)
    return (b + timedelta(days=days)).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────── 场景分类（确定性）
_BARGAIN = ("便宜", "优惠", "少点", "最低", "降", "折", "一口价", "卖不卖", "不卖我走")
_COMPLAIN = ("投诉", "碎了", "坏了", "质量", "怎么搞的", "退")
_HANDOFF = ("人工", "找老板", "转老板", "叫老板")
_PAY = ("付款", "转账", "打款", "付了", "汇款", "收款", "支付", "打过去", "已付")
_CHAT = ("心情", "吃了吗", "在吗", "忙不忙", "哈哈", "你好呀", "早上好", "晚上好", "辛苦", "天气")
_CATALOG = ("有什么", "目录", "有哪些", "价目")
_SAMPLE = ("打样", "样品", "寄样", "看样", "sample")
_LEAD = ("交期", "货期", "生产周期", "多久能出", "lead time", "leadtime", "delivery time", "how long", "production time")
_STOCKQ = ("还有多少", "多少库存", "库存多少", "剩多少", "有多少货", "存货多少", "how many in stock", "stock level")
_QTY = re.compile(r"\d+\s*(?:个|件|套|条|打|双|箱|只|台|支|包|袋|units?|pcs?|pieces?)|[一二两三四五六七八九十]+\s*(?:个|件|套|箱)")


def latin_heavy(text):
    letters = len(re.findall(r"[A-Za-z]", text or ""))
    return letters >= 8 and letters >= len(text or "") * 0.3


def classify_scenario(text, msg_type="text", is_boss=False):
    """返回 skill_loader 用的场景名之一。顺序即优先级，和 core.brain 的分支顺序一致。"""
    if is_boss:
        return "老板指令"
    if msg_type == "image":
        return "图片"
    t = (text or "").strip()
    if not t or t.startswith("__EVENT"):
        return "进店"
    if t.startswith("上新") or t.startswith("绑定老板"):
        return "老板指令"
    if any(k in t for k in _HANDOFF):
        return "转人工"
    if any(k in t for k in _COMPLAIN) and not t.startswith("上新"):
        return "投诉"
    if any(k in t for k in _PAY):
        return "付款"
    if any(k in t for k in _STOCKQ):
        return "问库存"
    if any(k in t.lower() for k in _SAMPLE):
        return "打样"
    if any(k in t.lower() for k in _LEAD):
        return "交期"
    if any(k in t for k in _BARGAIN) and not t.startswith("上新"):
        return "议价"
    if latin_heavy(t) or "FOB" in t.upper():
        return "外贸"
    if _QTY.search(t):
        return "下单"
    if any(k in t for k in _CATALOG) or "价" in t or "多少" in t or re.search(r"\b[A-Z]\d\b", t.upper()):
        return "首次询盘"
    if any(k in t for k in _CHAT):
        return "闲聊"
    return "其他"


# ─────────────────────────────────────────────── 写
def _append(rec):
    rec.setdefault("ts", now_iso())
    day = rec["ts"][:10]
    line = json.dumps(rec, ensure_ascii=False)
    try:
        with _LOCK:
            with open(log_dir() / f"{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        if _ERR_HOOK:
            try:
                _ERR_HOOK(e)
            except Exception:
                pass


def log_in(uid, kfid, text, msg_type="text", scenario="其他", is_boss=False):
    _append({"ev": "in", "uid": uid, "kf": kfid or "", "type": msg_type,
             "text": (text or "")[:MAX_TEXT], "scenario": scenario, "boss": bool(is_boss)})


def log_out(uid, kfid, text, scenario="其他", route="rule", is_boss=False):
    _append({"ev": "out", "uid": uid, "kf": kfid or "", "text": (text or "")[:MAX_TEXT],
             "scenario": scenario, "route": route, "boss": bool(is_boss)})


def log_pending(uid, pid, kind, desc, amount):
    _append({"ev": "pending", "uid": uid, "pid": pid, "kind": kind,
             "desc": (desc or "")[:MAX_TEXT], "amount": amount})


def log_act(uid, pid, kind, op, amount):
    _append({"ev": "act", "uid": uid, "pid": pid, "kind": kind, "op": op, "amount": amount})


def log_notify(uid, pid, ok, why, text):
    _append({"ev": "notify", "uid": uid, "pid": pid, "ok": bool(ok), "why": (why or "")[:200],
             "text": (text or "")[:MAX_TEXT]})


# ─────────────────────────────────────────────── 读
def read_day(day):
    p = log_dir() / f"{day}.jsonl"
    if not p.exists():
        return []
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def available_days():
    try:
        return sorted(p.stem for p in log_dir().glob("*.jsonl"))
    except Exception:
        return []
