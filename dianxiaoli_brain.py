# -*- coding: utf-8 -*-
"""
dianxiaoli_brain.py — 店小力 LLM 销售大脑（composable，借鉴 sales_brain v0.1.0 框架）

设计原则（三条护栏，决定了什么交给 LLM、什么绝不交给 LLM）：
  1. 红线由确定性代码守：库存保密／客户隐私／不冒充老板／超权议价 —— LLM 之前先拦，命中直接返回，不进模型。
  2. 价格与台账由代码算：LLM 只负责"说人话"，下单/断货登记/转人工通过结构化动作令牌回传，
     金额与库存扣减一律由代码按目录计算并落账；并对回复做「价格幻觉守卫」——出现目录外的价格即判定不可信。
  3. 逐层降级：DeepSeek → Claude → 规则引擎。没有 API key 时整层静默旁路，行为与规则版完全一致。

组合式钩子（对齐 sales_brain 框架）：
    escalation_checker  -> _red_line_check      命中即早返回，跳过 LLM
    knowledge_retriever -> _retrieve_knowledge  目录检索（只给可得性，不给库存数量）
    context_providers   -> _build_contexts      熟客档位 / 会话记忆 / 未结单
    response_parsers    -> _parse_actions       [ORDER][WAITLIST][ESCALATE] → 真实台账动作
"""
import json
import logging
import os
import re

log = logging.getLogger(__name__)

__version__ = "0.2.0"

LLM_ENABLED = os.environ.get("LLM_ENABLED", "1") not in ("0", "false", "False")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_FALLBACK = os.environ.get("CLAUDE_FALLBACK", "claude-haiku-4-5-20251001")
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "600"))
HISTORY_TURNS = 8          # 多轮上下文保留轮数（防上下文膨胀）

STATS = {"llm_ok": 0, "llm_fail": 0, "rule_fallback": 0, "price_guard_hits": 0,
         "red_line_hits": 0, "vision_ok": 0, "vision_fail": 0, "provider": "", "last_reason": ""}


# ══════════════════════════════════════════════════════════════════
# 系统提示词（借鉴美辉「同事口吻」方法论：严禁词 + 正反例 + 一次一问）
# ══════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """你是「{business_name}」的 AI 店员，名字叫小力。这是一家批发零售小微商户，顾客通过微信找你聊单。

# 你是谁
你不是客服机器人，你是店里的伙计。像个熟悉货、说话利索的同事那样聊天。

# 怎么说话（最重要）
- 短句、口语。一次 1–3 行，别写成邮件。
- 严禁这些客服套话：「欢迎光临」「很高兴为您服务」「为您解答」「请您提供以下信息」「感谢您的咨询」「祝您生活愉快」。
- 别每句都「您好」开头，别一上来罗列全部业务。
- 先接住对方的话，再问下一步；一次只问一个问题。
- 少用表情，一条最多一个，多数时候不用。

正例 → 「A3保温壶 零售45，50件起走拿货价32，有货。要几件？」
反例 → 「您好！欢迎咨询本店商品～我们的A3保温壶零售价为45元/件…如有需要请随时联系我们！」

正例 → 「实话说，这款断货了。给您登记，到货第一时间叫您。」
反例 → 「非常抱歉地通知您，该商品目前暂时缺货，给您带来不便敬请谅解。」

# 铁律（违反即事故）
1. 只依据下方【商品信息】回答。没有的货号/商品，直说没有，绝不编造价格或库存。
2. 绝不透露库存的**具体数量**。只说「有货」「不多了」「断货了」。顾客追问具体数量，就说不方便对外讲，反问他要多少。
3. 绝不泄露其他客户的价格或订单。
4. 你是 AI 店员，不是老板本人。底价、超权折扣、投诉处理一律转老板。
5. 报价只能用【商品信息】里给出的数字，一分钱都不许改。需要算总价时按给出的单价乘数量。

# 动作令牌（重要）
当对话产生下列结果时，在回复的**最后一行单独**输出令牌，系统会据此落账。顾客看不到令牌。
- 顾客确认要买：[ORDER:货号:数量]        例：[ORDER:A3:60]
- 商品断货但顾客想要：[WAITLIST:货号:数量]  例：[WAITLIST:G6:100]
- 需要老板介入（超权议价/投诉/退款/大单）：[ESCALATE:原因]  例：[ESCALATE:两万件要求七折]
没有结果就不要输出令牌。一次最多一个令牌。

# 客户与会话上下文
{contexts}

# 商品信息（本次检索到的，价格以此为准）
{knowledge}
"""


# ══════════════════════════════════════════════════════════════════
# 1. escalation_checker —— 红线确定性拦截（LLM 之前）
# ══════════════════════════════════════════════════════════════════
def _red_line_check(text, core, d, uid):
    """命中红线返回成品话术（不进 LLM）；否则 None。"""
    t = text or ""

    # 库存数量保密
    if any(k in t for k in ["还有多少", "多少库存", "库存多少", "剩多少", "有多少货", "存货多少"]):
        it = core.find_item(t, d)
        if it and core._stock_of(d, it["sku"]) <= 0:
            return f"{it['name']} 现在断货了，要的话我给您登记，到货就通知您。"
        if it:
            return f"{it['name']} 有货的，具体存量不方便对外说哈。您要多少？我看看能不能一次给您发齐。"
        return "具体库存数不方便对外说哈。您说要哪款、要多少，我直接告诉您能不能发齐。"

    # 他人隐私
    others = [n for u, n in d.get("regulars", {}).items() if u != uid]
    if (any(n in t for n in others) and any(k in t for k in ["什么价", "订单", "拿多少", "发我"])) \
       or any(k in t for k in ["别人的订单", "其他客户", "她的订单", "他的订单"]):
        return "别人的价保密，不能说；您的价我同样不会跟别人讲。您要多少？我按量给您算。"

    # 冒充老板 / 套底价
    if ("你就是老板" in t or "底价" in t) and uid != d.get("boss_userid"):
        return ("我是店里的AI店员小力，不是老板。底价得老板点头。"
                "您说个量吧，100件以上我这儿能直接给到最优；再大的量我这就去问老板。")

    # 竞对打探供应链
    if any(k in t for k in ["哪个厂", "厂家电话", "供应商", "进货渠道", "成本价"]):
        return "这个不方便说哈。货您放心比，长期做价格好谈。"
    return None


# ══════════════════════════════════════════════════════════════════
# 2. knowledge_retriever —— 目录检索（红线内建：不吐库存数量）
# ══════════════════════════════════════════════════════════════════
def _retrieve_knowledge(text, core, d, uid, k=6):
    cat = core.get_catalog(d)
    hit = core.find_item(text, d)
    picked, seen = [], set()
    if hit:
        picked.append(hit); seen.add(hit["sku"])
    # 文本中显式出现的货号
    for sku in re.findall(r"\b([A-Z]\d)\b", (text or "").upper()):
        c = next((x for x in cat if x["sku"] == sku), None)
        if c and c["sku"] not in seen:
            picked.append(c); seen.add(c["sku"])
    # 会话记忆里的上一款
    last = core._recall_item(d, uid)
    if last and last["sku"] not in seen:
        picked.append(last); seen.add(last["sku"])
    # 补足到 k 条（让 LLM 有推荐替代品的余地）
    for c in cat:
        if len(picked) >= k:
            break
        if c["sku"] not in seen:
            picked.append(c); seen.add(c["sku"])

    is_regular = uid in d.get("regulars", {})
    lines, allowed = [], set()
    for c in picked:
        stock = core._stock_of(d, c["sku"])
        avail = "断货" if stock <= 0 else ("有货但不多了" if stock <= 5 else "有货")
        moq = c.get("moq", core.MOQ)
        lines.append(
            f"- {c['sku']} {c['name']}｜零售 ¥{c['retail']:g}/件｜{moq}件起拿货价 ¥{c['trade']:g}/件"
            f"｜可得性：{avail}" + ("｜该顾客是熟客，直接适用拿货价" if is_regular else "")
        )
        allowed.update({c["retail"], c["trade"], round(c["trade"] * 0.95, 1)})
    lines.append(f"（起批量 {core.MOQ} 件；100 件以上你有权给到拿货价 95 折；再低必须转老板）")
    lines.append(f"（配送政策：{core.SHIPPING_POLICY}）")
    return "\n".join(lines), allowed, picked


# ══════════════════════════════════════════════════════════════════
# 3. context_providers —— 熟客档位 / 会话记忆 / 未结单
# ══════════════════════════════════════════════════════════════════
def _build_contexts(core, d, uid):
    out = []
    who = d.get("regulars", {}).get(uid)
    out.append(f"顾客身份：熟客「{who}」，享拿货价" if who else "顾客身份：新客（未建立熟客关系）")
    last = core._recall_item(d, uid)
    if last:
        out.append(f"刚才在聊：{last['sku']} {last['name']}（顾客说「这个/那个」多半指它）")
    mine = [p for p in d.get("pending", []) if p.get("userid") == uid]
    if mine:
        out.append("该顾客未结事项：" + "；".join(f"#{p['id']} {p['kind']} {p['desc']}" for p in mine[-3:]))
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════
# 4. response_parsers —— 动作令牌 → 真实台账动作
# ══════════════════════════════════════════════════════════════════
TOKEN_RE = re.compile(r"\[(ORDER|WAITLIST|ESCALATE)\s*:\s*([^\]]+)\]", re.I)


def _parse_actions(reply, core, d, uid, msg=None):
    """抽出令牌 → 执行动作（价格由代码算）→ 返回(清洗后文本, 动作摘要)"""
    actions = []
    for m in TOKEN_RE.finditer(reply):
        kind, arg = m.group(1).upper(), m.group(2).strip()
        parts = [p.strip() for p in arg.split(":")]
        try:
            if kind == "ORDER" and len(parts) >= 2:
                sku, qty = parts[0].upper(), int(re.sub(r"\D", "", parts[1]) or 0)
                item = next((c for c in core.get_catalog(d) if c["sku"] == sku), None)
                if not item or qty <= 0:
                    continue
                stock = core._stock_of(d, sku)
                if stock <= 0:
                    d["waitlist"].append({"userid": uid, "sku": sku, "ts": core.now_str()})
                    actions.append(("waitlist", sku, qty)); continue
                moq = item.get("moq", core.MOQ)
                price = item["trade"] if (qty >= moq or uid in d.get("regulars", {})) else item["retail"]
                tier = "拿货价" if price == item["trade"] else "零售价"
                total = price * qty
                desc = f"{item['name']} ×{qty} @¥{price:g}（{tier}）"
                pid = core.add_pending(d, uid, d.get("regulars", {}).get(uid, "顾客"), desc, total,
                                       "订单核准", kfid=getattr(msg, "account_id", ""))
                actions.append(("order", pid, desc, total))
            elif kind == "WAITLIST" and len(parts) >= 1:
                sku = parts[0].upper()
                d["waitlist"].append({"userid": uid, "sku": sku, "ts": core.now_str()})
                actions.append(("waitlist", sku, 0))
            elif kind == "ESCALATE":
                pid = core.add_pending(d, uid, d.get("regulars", {}).get(uid, "顾客"),
                                       f"转老板：{arg[:40]}", 0, "转人工", kfid=getattr(msg, "account_id", ""))
                actions.append(("escalate", pid, arg[:40]))
        except Exception as e:  # 单个令牌失败不影响回复
            log.warning(f"action token failed: {e}")
    clean = TOKEN_RE.sub("", reply).strip()
    return clean, actions


# ══════════════════════════════════════════════════════════════════
# 5. 价格幻觉守卫
# ══════════════════════════════════════════════════════════════════
def _price_guard(text, allowed):
    """回复里出现的每个价格都必须来自目录（或由目录价算出的合计）。否则判定不可信。"""
    nums = re.findall(r"[¥￥]\s*([\d,]+(?:\.\d+)?)", text or "")
    if not nums:
        return True
    base = set()
    for a in allowed:
        base.add(round(float(a), 2))
    for raw in nums:
        v = round(float(raw.replace(",", "")), 2)
        if v in base:
            continue
        # 合计：单价 × 整数数量
        if any(a > 0 and abs(v / a - round(v / a)) < 1e-6 and 1 <= round(v / a) <= 100000 for a in base if a):
            continue
        return False
    return True


# ══════════════════════════════════════════════════════════════════
# 5.5 图片理解：付款凭证 / 商品照 / 其他
# ══════════════════════════════════════════════════════════════════
VISION_PROMPT = """你在给一家批发零售店做图片分类。看这张顾客发来的图，判断它属于哪一类，并按 JSON 回答，不要任何多余文字。

分类：
- payment：付款凭证/转账截图/收款码回执（特征：支付宝、微信支付、转账成功、收款方、金额、订单号、银行 App 界面）
- product：商品照片（顾客拍的实物、货架、商品图，想问这个货）
- other：其他（人像、风景、聊天截图、看不清等）

店里在售商品（判断 product 时，从中选最像的一个，选不出就留空）：
{catalog}

只输出 JSON：
{{"type":"payment|product|other","amount":数字或null,"sku":"货号或空","confidence":0到1的小数,"note":"一句话理由"}}"""


def _vision_available():
    return bool(_router.mock or ANTHROPIC_KEY)


def classify_image(image_bytes, core, d):
    """返回 {'type','amount','sku','confidence','note'}；无视觉能力时返回 None。"""
    if not LLM_ENABLED or not _vision_available() or not image_bytes:
        return None
    cat = core.get_catalog(d)
    catalog = "\n".join(f"- {c['sku']} {c['name']}" for c in cat[:20])
    prompt = VISION_PROMPT.format(catalog=catalog)
    try:
        if _router.mock:
            raw = _router.mock(prompt, [{"role": "user", "content": "[image]"}])
        else:
            import base64, requests
            b64 = base64.b64encode(image_bytes).decode()
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": CLAUDE_MODEL, "max_tokens": 300,
                      "messages": [{"role": "user", "content": [
                          {"type": "image", "source": {"type": "base64",
                                                       "media_type": "image/jpeg", "data": b64}},
                          {"type": "text", "text": prompt}]}]},
                timeout=25)
            r.raise_for_status()
            raw = "".join(x.get("text", "") for x in r.json().get("content", []))
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        out = json.loads(m.group(0))
        STATS["vision_ok"] = STATS.get("vision_ok", 0) + 1
        return {"type": (out.get("type") or "other").lower(),
                "amount": out.get("amount"),
                "sku": (out.get("sku") or "").upper().strip(),
                "confidence": float(out.get("confidence") or 0),
                "note": out.get("note", "")}
    except Exception as e:
        STATS["vision_fail"] = STATS.get("vision_fail", 0) + 1
        STATS["last_reason"] = f"vision: {e}"[:200]
        log.warning(f"vision classify failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# 6. LLM 路由：DeepSeek → Claude →（上层再降级到规则引擎）
# ══════════════════════════════════════════════════════════════════
class LLMRouter:
    def __init__(self, mock=None):
        self.mock = mock

    def available(self):
        return bool(self.mock or DEEPSEEK_KEY or ANTHROPIC_KEY)

    def chat(self, system_prompt, messages):
        if self.mock:
            STATS["provider"] = "mock"
            return self.mock(system_prompt, messages)
        if DEEPSEEK_KEY:
            try:
                out = self._deepseek(system_prompt, messages)
                STATS["provider"] = "deepseek"
                return out
            except Exception as e:
                STATS["last_reason"] = f"deepseek: {e}"
                log.warning(f"deepseek failed, falling back: {e}")
        if ANTHROPIC_KEY:
            out = self._claude(system_prompt, messages)
            STATS["provider"] = "claude"
            return out
        raise RuntimeError("no llm key configured")

    def _deepseek(self, system_prompt, messages):
        import requests
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json={"model": DEEPSEEK_MODEL, "max_tokens": MAX_TOKENS, "temperature": 0.3,
                  "messages": [{"role": "system", "content": system_prompt}] + messages},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _claude(self, system_prompt, messages):
        import requests
        for model in (CLAUDE_MODEL, CLAUDE_FALLBACK):
            try:
                r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": model, "max_tokens": MAX_TOKENS, "system": system_prompt,
                          "messages": messages},
                    timeout=25,
                )
                r.raise_for_status()
                return "".join(b.get("text", "") for b in r.json().get("content", []))
            except Exception as e:
                STATS["last_reason"] = f"claude/{model}: {e}"
                continue
        raise RuntimeError("claude failed on both models")


_router = LLMRouter()


def set_mock(fn):
    """测试用：注入假 LLM"""
    global _router
    _router = LLMRouter(mock=fn)


def llm_available():
    return LLM_ENABLED and _router.available()


# ══════════════════════════════════════════════════════════════════
# 7. 主入口：返回文本或 None（None = 交回规则引擎）
# ══════════════════════════════════════════════════════════════════
def think(msg, core, d, business_name="店小力"):
    """LLM 大脑。返回 (text, actions) 或 None（表示应降级到规则引擎）。"""
    if not llm_available():
        return None
    text, uid = (msg.text or "").strip(), msg.sender_id
    if not text or text.startswith("__EVENT"):
        return None

    # 1) 红线早拦截
    hit = _red_line_check(text, core, d, uid)
    if hit:
        STATS["red_line_hits"] += 1
        return hit, []

    # 2/3) 检索 + 上下文
    knowledge, allowed, picked = _retrieve_knowledge(text, core, d, uid)
    contexts = _build_contexts(core, d, uid)
    system_prompt = SYSTEM_PROMPT.format(business_name=business_name,
                                         contexts=contexts or "（无）",
                                         knowledge=knowledge)

    # 多轮历史（限轮数，防膨胀）
    hist = core._mem(d, uid).get("history", [])[-HISTORY_TURNS:]
    messages = [{"role": h["role"], "content": h["content"]} for h in hist]
    messages.append({"role": "user", "content": text})

    # 4) 调模型
    try:
        raw = _router.chat(system_prompt, messages)
        STATS["llm_ok"] += 1
    except Exception as e:
        STATS["llm_fail"] += 1
        STATS["last_reason"] = str(e)[:200]
        log.warning(f"LLM unavailable, rule fallback: {e}")
        return None

    # 5) 动作令牌 → 真实台账
    clean, actions = _parse_actions(raw, core, d, uid, msg)

    # 6) 价格幻觉守卫
    if not _price_guard(clean, allowed):
        STATS["price_guard_hits"] += 1
        STATS["last_reason"] = "price_guard: 回复含目录外价格，已弃用 LLM 结果"
        log.warning("price guard tripped, rule fallback")
        return None

    if not clean:
        return None

    # 7) 写回会话记忆（含轮次历史）
    s = core._mem(d, uid)
    s.setdefault("history", [])
    s["history"] = (s["history"] + [{"role": "user", "content": text},
                                    {"role": "assistant", "content": clean}])[-HISTORY_TURNS * 2:]
    if picked:
        core._remember(d, uid, picked[0])
    core.save(d)
    return clean, actions


def status():
    return {"llm_enabled": LLM_ENABLED, "keys": {"deepseek": bool(DEEPSEEK_KEY), "anthropic": bool(ANTHROPIC_KEY)},
            "available": llm_available(), "vision": _vision_available(), **STATS}
