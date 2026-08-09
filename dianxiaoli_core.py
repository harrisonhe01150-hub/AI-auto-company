# -*- coding: utf-8 -*-
"""
dianxiaoli_core.py — 店小力样板店：销售大脑 + 老板端控制台 + 自检
放仓库根目录，与 wecom_core/ 同级。演示级实现：确定性规则大脑（生产版接 RAG+LLM），
数据落地单文件 JSON（Railway 重启会重置——演示够用，生产换数据库）。

老板端: GET /boss?key=<BOSS_KEY>   （手机浏览器可开）
自检:   GET /status
"""
import json, os, re, threading, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from wecom_core import OutboundReply

CN_TZ = timezone(timedelta(hours=8))
DATA_PATH = Path("dianxiaoli_data.json")
_LOCK = threading.Lock()
LAST_ERROR = {"text": "", "hint": "", "ts": ""}

BOSS_KEY = os.environ.get("BOSS_KEY", "xiaoli888")

# ── 商品目录（义乌风格演示库存, 双价体系: 零售价/拿货价, 50件起批）──
CATALOG = [
    {"sku": "A3", "name": "A3保温壶500ml", "retail": 45, "trade": 32, "stock": 140},
    {"sku": "B1", "name": "B1玻璃杯", "retail": 12, "trade": 8, "stock": 800},
    {"sku": "C2", "name": "C2吸管保温杯", "retail": 39, "trade": 26, "stock": 220},
    {"sku": "D5", "name": "D5快充数据线1m", "retail": 15, "trade": 6.5, "stock": 1500},
    {"sku": "E8", "name": "E8棉袜(10双装)", "retail": 25, "trade": 15, "stock": 600},
    {"sku": "F1", "name": "F1自动雨伞", "retail": 35, "trade": 22, "stock": 90},
    {"sku": "G6", "name": "G6帆布购物袋", "retail": 18, "trade": 9, "stock": 0},
    {"sku": "H2", "name": "H2陶瓷马克杯", "retail": 22, "trade": 13, "stock": 460},
    {"sku": "J9", "name": "J9LED小台灯", "retail": 49, "trade": 33, "stock": 75},
    {"sku": "K4", "name": "K4蓝牙音箱mini", "retail": 89, "trade": 62, "stock": 58},
    {"sku": "L7", "name": "L7保鲜盒3件套", "retail": 29, "trade": 18, "stock": 310},
    {"sku": "M3", "name": "M3珊瑚绒毛巾", "retail": 16, "trade": 8.5, "stock": 720},
    {"sku": "B2", "name": "B2玻璃杯高款", "retail": 15, "trade": 9.5, "stock": 0},
    {"sku": "D1", "name": "D1连衣裙M码", "retail": 89, "trade": 55, "stock": 2},
]
MOQ = 50  # 拿货价起批量
SHIPPING_POLICY = "同城（江东/福田等街道）满¥500免运费，不满到付；跨省走物流按体积计费，大宗单可谈包邮。"


# ── 数据存取 ────────────────────────────────────────────────
def _default_data():
    return {
        "ai_on": True,
        "boss_userid": "",
        "regulars": {},          # userid -> 称呼(熟客享拿货价)
        "stock": {c["sku"]: c["stock"] for c in CATALOG},
        "pending": [],           # 待核准 {id,userid,name,desc,amount,kind,ts}
        "orders": [],            # 已核准成交 {id,userid,desc,amount,cost,ts}
        "waitlist": [],          # 断货登记 {userid,sku,ts}
        "customers": {},         # 新客画像建档
        "custom_skus": [],       # 老板上新的SKU
        "seq": 1,
    }


def load():
    with _LOCK:
        if DATA_PATH.exists():
            try:
                return json.loads(DATA_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return _default_data()


def save(d):
    with _LOCK:
        DATA_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def now_str():
    return datetime.now(CN_TZ).strftime("%m-%d %H:%M")


def today_str():
    return datetime.now(CN_TZ).strftime("%m-%d")


def record_error(exc: BaseException):
    text = f"{type(exc).__name__}: {exc}"
    hint = ""
    m = re.search(r"errcode=60020.*from ip: ([0-9.]+)", str(exc))
    if m:
        hint = f"把出口IP {m.group(1)} 追加到企微后台「企业可信IP」（英文分号分隔），保存后重试即可。"
    elif "errcode=95000" in str(exc):
        hint = "open_kfid 无效：确认微信客服账号仍在「通过API管理」名单中。"
    elif "errcode=40014" in str(exc) or "errcode=42001" in str(exc):
        hint = "access_token 失效：检查 WECOM_KF_SECRET 是否正确。"
    LAST_ERROR.update({"text": text, "hint": hint, "ts": now_str()})


# ── 销售大脑（确定性演示版；生产版换 RAG+LLM 入口）──────────
def get_catalog(d):
    return CATALOG + d.get("custom_skus", [])


def find_item(text, d=None):
    cat = get_catalog(d) if d else CATALOG
    for c in cat:
        if c["sku"].lower() in text.lower() or c["name"] in text:
            return c
    kw = {"保温壶": "A3", "茶壶": "A3", "水壶": "A3", "kettle": "A3",
          "玻璃杯": "B1", "glass cup": "B1", "吸管": "C2", "数据线": "D5",
          "袜": "E8", "伞": "F1", "帆布": "G6", "购物袋": "G6", "马克杯": "H2",
          "台灯": "J9", "音箱": "K4", "保鲜盒": "L7", "饭盒": "L7", "毛巾": "M3",
          "裙": "D1", "杯子": "B1"}
    for k, sku in kw.items():
        if k.lower() in text.lower():
            return next(c for c in cat if c["sku"] == sku)
    return None


def parse_qty(text):
    m = re.search(r"(\d+)\s*(?:个|件|套|条|打|双|箱|units?|pcs?)", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"[来要拿](\s*)(\d+)(?![折%])", text)
    return int(m.group(2)) if m else None


def price_for(userid, item, d):
    if userid in d["regulars"]:
        return item["trade"], "拿货价"
    return item["retail"], "零售价"


def add_pending(d, userid, name, desc, amount, kind):
    pid = d["seq"]; d["seq"] += 1
    d["pending"].append({"id": pid, "userid": userid, "name": name,
                         "desc": desc, "amount": amount, "kind": kind, "ts": now_str()})
    return pid


def _stock_of(d, sku):
    return d["stock"].get(sku, 0)


def _fmt_stock_tail(stock):
    return f"仅剩 {stock} 件" if stock <= 5 else f"现货 {stock}"


def _parse_multi(text, d):
    """按逗号/顿号切段, 每段解析 (item, qty); ≥2 项视为混合下单"""
    parts = re.split(r"[，,、;；]|再加", text)
    got = []
    for p in parts:
        it, q = find_item(p, d), parse_qty(p)
        if it and q:
            got.append((it, q))
    # 去重同SKU保留后者
    uniq = {}
    for it, q in got:
        uniq[it["sku"]] = (it, q)
    return list(uniq.values())


def _latin_heavy(text):
    letters = len(re.findall(r"[A-Za-z]", text))
    return letters >= 8 and letters >= len(text) * 0.3


def boss_report(d):
    today = today_str()
    orders = [o for o in d["orders"] if o["ts"].startswith(today)]
    gmv = sum(o["amount"] for o in orders)
    profit = sum(o["amount"] - o.get("cost", 0) for o in orders)
    cat = get_catalog(d)
    low = [f'{c["name"]}×{d["stock"].get(c["sku"], 0)}' for c in cat
           if 0 < d["stock"].get(c["sku"], 0) <= 80]
    out = [f'{c["name"]}' for c in cat if d["stock"].get(c["sku"], 0) == 0]
    lines = [f"📊 今日经营（{today}）",
             f"成交 {len(orders)} 单 ｜ 金额 ¥{gmv:,.0f} ｜ 毛利 ¥{profit:,.0f}",
             f"待核准 {len(d['pending'])} 笔"]
    if low: lines.append("⚠️ 低库存: " + "、".join(low))
    if out: lines.append("❌ 断货: " + "、".join(out) + f"（{len(d['waitlist'])} 人登记等货）")
    if out or low: lines.append("💡 建议: 优先补断货与低库存款，断货款有登记客户可定向通知到货。")
    return "\n".join(lines)


def _week_top(d):
    agg = {}
    for o in d["orders"]:
        m = re.search(r"([A-Z]\d)[^×]*×(\d+)", o["desc"])
        if m:
            agg[m.group(1)] = agg.get(m.group(1), 0) + int(m.group(2))
    if not agg:
        return "本周暂无成交记录，台账里还没有数据～"
    cat = {c["sku"]: c["name"] for c in get_catalog(d)}
    top = sorted(agg.items(), key=lambda x: -x[1])[:3]
    return "本周销量TOP（按台账）：\n" + "\n".join(
        f"{i+1}. {cat.get(s, s)} ×{n}" for i, (s, n) in enumerate(top))


def _owner_brain(text, d):
    """老板端大白话指令; 返回 OutboundReply 或 None(转顾客逻辑)"""
    if "卖得怎么样" in text or "日报" in text:
        return OutboundReply(text=boss_report(d))
    if "卖得最好" in text or "什么卖得" in text:
        return OutboundReply(text=_week_top(d))
    # 完整上新: 上新：不锈钢饭盒 C5，进价 12，零售 25，批发 18 起批 50 个，来了 300 个
    if text.startswith("上新") and ("零售" in text or "进价" in text):
        sku_m = re.search(r"([A-Z]\d)", text)
        name_m = re.search(r"上新[：:，,\s]*([\u4e00-\u9fffA-Za-z0-9]+?)\s*[A-Z]\d", text)
        cost = re.search(r"进价\s*(\d+(?:\.\d+)?)", text)
        retail = re.search(r"零售\s*(\d+(?:\.\d+)?)", text)
        trade = re.search(r"批发\s*(\d+(?:\.\d+)?)", text)
        qty = re.search(r"来了\s*(\d+)", text)
        moq = re.search(r"起批\s*(\d+)", text)
        if sku_m and retail:
            sku = sku_m.group(1)
            item = {"sku": sku,
                    "name": f"{sku}{name_m.group(1)}" if name_m else sku,
                    "retail": float(retail.group(1)),
                    "trade": float(trade.group(1)) if trade else round(float(retail.group(1)) * 0.7, 1),
                    "cost": float(cost.group(1)) if cost else 0,
                    "moq": int(moq.group(1)) if moq else MOQ,
                    "stock": 0}
            d["custom_skus"] = [c for c in d.get("custom_skus", []) if c["sku"] != sku] + [item]
            d["stock"][sku] = int(qty.group(1)) if qty else 0
            save(d)
            return OutboundReply(text=(
                f"✅ 已建档上新：{item['name']} ｜ 进价¥{item['cost']:g} ｜ 零售¥{item['retail']:g} ｜ "
                f"批发¥{item['trade']:g}（{item['moq']}个起批）｜ 入库 {d['stock'][sku]} 个。有误随时说「上新」重报。"))
    # 简单补货: 上新A3 100个
    m = re.search(r"上新\s*([A-Za-z]\d)\s*(\d+)", text)
    if m:
        sku, n = m.group(1).upper(), int(m.group(2))
        d["stock"][sku] = d["stock"].get(sku, 0) + n; save(d)
        item = next((c for c in get_catalog(d) if c["sku"] == sku), None)
        return OutboundReply(text=f"✅ {item['name'] if item else sku} 库存 +{n} → {d['stock'][sku]}")
    # 大白话记账: 刚卖了 5 个保温壶给老张，走的拿货价
    m = re.search(r"卖了\s*(\d+)\s*[个件套]?\s*", text)
    if m and ("卖了" in text):
        it = find_item(text, d)
        if it:
            q = int(m.group(1))
            tier = "拿货价" if ("拿货" in text or "批发" in text) else "零售价"
            price = it["trade"] if tier == "拿货价" else it["retail"]
            buyer_m = re.search(r"给\s*([\u4e00-\u9fff]{1,6})", text)
            buyer = buyer_m.group(1) if buyer_m else "散客"
            d["stock"][it["sku"]] = max(0, d["stock"].get(it["sku"], 0) - q)
            amount = price * q
            d["orders"].append({"id": d["seq"], "userid": "offline", 
                                "desc": f"{it['name']} ×{q} @¥{price}（{tier}·{buyer}）",
                                "amount": amount, "cost": round(it.get("cost", it["trade"] * 0.8) * q, 1),
                                "ts": now_str()})
            d["seq"] += 1; save(d)
            return OutboundReply(text=(
                f"✅ 已记账：{it['name']} ×{q} @¥{price:g}（{tier}）给{buyer}，合计 ¥{amount:,.0f}。"
                f"{it['name']} 现货余 {d['stock'][it['sku']]}。"))
    if "关闭AI" in text.replace(" ", ""):
        d["ai_on"] = False; save(d)
        return OutboundReply(text="🔕 AI 接待已关闭（顾客消息将只进工作台）")
    if "开启AI" in text.replace(" ", ""):
        d["ai_on"] = True; save(d)
        return OutboundReply(text="🔔 AI 接待已开启")
    return None


def brain(msg):
    """wecom_core.InboundMessage -> OutboundReply | None"""
    d = load()
    text, uid = (msg.text or "").strip(), msg.sender_id

    # 新客画像建档
    if uid and uid not in d.get("customers", {}):
        d.setdefault("customers", {})[uid] = now_str(); save(d)

    # 老板绑定与老板指令
    if text.startswith("绑定老板"):
        if BOSS_KEY and BOSS_KEY in text:
            d["boss_userid"] = uid; save(d)
            return OutboundReply(text="✅ 老板身份已绑定。可直接说：今天卖得怎么样 / 卖得最好的是什么 / 上新A3 100个 / 刚卖了5个保温壶给老张拿货价 / 关闭AI")
        return OutboundReply(text="口令不对，格式：绑定老板 你的BOSS_KEY")
    if uid and uid == d.get("boss_userid"):
        r = _owner_brain(text, d)
        if r:
            return r

    if not d.get("ai_on", True):
        return None

    # 进店欢迎
    if text == "__EVENT_ENTER_SESSION__":
        who = d["regulars"].get(uid)
        hello = f"{who}，欢迎回来！" if who else "您好，我是店小力的AI店员小力～"
        return OutboundReply(text=hello + "本店批发零售双价，50件起享拿货价。想看什么直接说，比如「A3保温壶什么价」。")

    # 付款截图 → 金额核对 → 待核准
    if msg.msg_type == "image":
        ocr_amount = 0
        try:
            ocr_amount = float((msg.raw or {}).get("demo_ocr_amount", 0))
        except Exception:
            pass
        my_orders = [p for p in d["pending"] if p["userid"] == uid and p["kind"] == "订单核准"]
        if ocr_amount and my_orders:
            latest = my_orders[-1]
            if abs(ocr_amount - latest["amount"]) < 0.01:
                add_pending(d, uid, d["regulars"].get(uid, "顾客"),
                            f"付款截图 ¥{ocr_amount:,.0f} 与单 #{latest['id']} 金额一致", ocr_amount, "付款核验"); save(d)
                return OutboundReply(text=f"收到您的付款截图📷 金额 ¥{ocr_amount:,.0f} 与订单核对一致，已提交老板核准，核准后马上发货～")
            diff = latest["amount"] - ocr_amount
            add_pending(d, uid, d["regulars"].get(uid, "顾客"),
                        f"⚠️ 金额异常: 截图¥{ocr_amount:,.0f} vs 订单¥{latest['amount']:,.0f}（差¥{diff:,.0f}）", ocr_amount, "金额异常"); save(d)
            return OutboundReply(text=(
                f"收到截图📷 核对到金额 ¥{ocr_amount:,.0f} 与订单 ¥{latest['amount']:,.0f} 有出入（差 ¥{diff:,.0f}），"
                "已标记给老板确认怎么处理，请稍等——发货安排以老板核准为准哈。"))
        add_pending(d, uid, d["regulars"].get(uid, "顾客"),
                    "付款截图（金额待核对）", 0, "付款核验"); save(d)
        return OutboundReply(text="收到您的付款截图📷 正在核对金额，老板核准后马上安排发货～")

    # 幻觉红线: 明确SKU码但不在目录
    sku_tokens = re.findall(r"\b([A-Z]\d)\b", text.upper())
    cat_skus = {c["sku"] for c in get_catalog(d)}
    unknown = [s for s in sku_tokens if s not in cat_skus]
    if unknown and not any(s in cat_skus for s in sku_tokens):
        near = find_item(text, d)
        rec = f"相近的有 {near['name']}（零售¥{near['retail']:g}）可以了解下～" if near else "可以说下商品类目，我帮您找相近的现货。"
        return OutboundReply(text=f"咱家没有 {unknown[0]} 这个货号哦，不敢乱报价格🙅 {rec}")

    # 客户隐私红线 (只拦"打听他人", 不拦本人自称)
    other_names = [n for _u, n in d["regulars"].items() if _u != uid]
    if any(n in text for n in other_names) and any(k in text for k in ["什么价", "订单", "拿多少", "发我"]):
        return OutboundReply(text="其他客户的价格和订单是保密的哈🙊 每位客户的信息我们都不外泄——您的信息同样如此。您要拿货我按您的量给您最合适的价。")
    if any(k in text for k in ["别人的订单", "其他客户", "她的订单", "他的订单"]):
        return OutboundReply(text="客户信息保密是我们的规矩哈🙊 不过您的量到了，价格一定给到位。")

    # 冒充老板/套底价
    if ("你就是老板" in text or "底价" in text) and not (uid == d.get("boss_userid")):
        return OutboundReply(text="我是店小力的AI店员小力，不是老板本人哈😄 底价权限在老板那里——您说个量，100件以上我可以直接给授权内的最优价，更大的量我马上帮您问老板。")

    # 竞对打探
    if any(k in text for k in ["哪个厂", "厂家电话", "供应商", "进货渠道", "成本价"]):
        return OutboundReply(text="供应渠道是咱家的商业信息，不方便透露啦😄 货的品质价格您放心比，长期合作价格好谈。")

    # 退款/投诉/人工
    if any(k in text for k in ["退", "投诉", "碎了", "坏了", "质量", "怎么搞的"]) and msg.msg_type == "text" and not text.startswith("上新"):
        if any(k in text for k in ["投诉", "碎了", "坏了", "怎么搞的", "质量"]):
            add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"投诉/售后：{text[:40]}", 0, "转人工"); save(d)
            return OutboundReply(text="实在抱歉给您添麻烦了🙏 这个问题我已第一时间转给老板本人，并附上了您的订单记录，马上给您处理方案，一定负责到底。")
        add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"退换请求：{text[:40]}", 0, "转人工"); save(d)
        return OutboundReply(text="收到～别着急，退换的事老板会亲自跟进，我已把您的订单信息一并转过去了，很快回复您🙏")
    if any(k in text for k in ["人工", "找老板", "转老板"]):
        add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"顾客请求人工：{text[:40]}", 0, "转人工"); save(d)
        return OutboundReply(text="好的，已把完整对话转给老板本人，马上回复您🙏")

    # 配送政策
    if any(k in text for k in ["送到", "运费", "配送", "包邮", "发货到", "ship"]) and not _latin_heavy(text):
        return OutboundReply(text=f"配送政策：{SHIPPING_POLICY} 您在哪个片区/要发哪里？我帮您算一下。")

    # 英文/外贸 (义乌场景)
    if _latin_heavy(text) or "FOB" in text.upper():
        it = find_item(text, d)
        if "FOB" in text.upper():
            if it:
                q = parse_qty(text) or 0
                usd = round(it["trade"] / 7.2 * 1.22, 2)  # 拿货价+出口包装港杂
                return OutboundReply(text=(
                    f"FOB quote for {it['name']} ({q or 'your qty'} pcs): US${usd}/pc, FOB Ningbo/Yiwu. "
                    f"Includes export packing & port charges. MOQ 500 pcs for FOB terms. "
                    f"Please confirm port & target qty to lock the quote. 也可以中文聊～"))
            return OutboundReply(text="Sure — please share the SKU/item and quantity, I'll quote FOB Ningbo/Yiwu right away (MOQ 500 pcs for FOB terms).")
        if it:
            stock = _stock_of(d, it["sku"])
            usd = round(it["trade"] / 7.2, 2)
            return OutboundReply(text=(
                f"Hi! {it['name']}: MOQ {MOQ} pcs at trade price ¥{it['trade']:g} (≈US${usd})/pc, {stock} in stock. "
                f"Yes we ship worldwide (Lagos routes available) — sea/air freight quoted by volume. "
                f"中文/English 都可以聊～"))
        return OutboundReply(text="Hi! We ship worldwide. Tell me the item (SKU or name) and quantity, I'll quote right away — 中英文都可以～")

    # 混合下单 (≥2 SKU)
    multi = _parse_multi(text, d)
    if len(multi) >= 2:
        lines, total = [], 0
        for it, q in multi:
            price = it["trade"] if (q >= it.get("moq", MOQ) or uid in d["regulars"]) else it["retail"]
            lines.append(f"{it['name']} ×{q} @¥{price:g} = ¥{price*q:,.0f}")
            total += price * q
        desc = "；".join(f"{it['sku']}×{q}" for it, q in multi)
        pid = add_pending(d, uid, d["regulars"].get(uid, "顾客"),
                          f"混合订单 {desc} 合计¥{total:,.0f}", total, "订单核准"); save(d)
        return OutboundReply(text="给您列一下：\n" + "\n".join(lines) +
                             f"\n合计 ¥{total:,.0f}（单号 #{pid}）。确认的话发付款截图，老板核准后一起发货～")

    # 改单
    m = re.search(r"([A-Z]\d)\s*改成?\s*(\d+)\s*个?", text.upper())
    if m and ("改" in text):
        sku, newq = m.group(1), int(m.group(2))
        my_orders = [p for p in d["pending"] if p["userid"] == uid and p["kind"] == "订单核准" and sku in p["desc"]]
        if my_orders:
            p = my_orders[-1]
            it = next((c for c in get_catalog(d) if c["sku"] == sku), None)
            if it:
                # 重算: 简化为该SKU行重报
                price = it["trade"] if (newq >= MOQ or uid in d["regulars"]) else it["retail"]
                old_amount = p["amount"]
                m2 = re.search(rf"{sku}×(\d+)", p["desc"])
                if m2:
                    oldq = int(m2.group(1))
                    p["desc"] = p["desc"].replace(f"{sku}×{oldq}", f"{sku}×{newq}")
                    p["amount"] = round(old_amount - price * oldq + price * newq, 1)
                else:
                    p["desc"] += f"（{sku}改为×{newq}）"
                    p["amount"] = round(price * newq, 1)
                save(d)
                return OutboundReply(text=f"改好了：单 #{p['id']} 现在是 {p['desc']}，新合计 ¥{p['amount']:,.0f}。确认发截图就行～")
        return OutboundReply(text="您说的这单我没找到待处理记录🤔 麻烦说下单号或重新报一遍数量，我给您重开一单。")

    item = find_item(text, d)
    qty = parse_qty(text)

    # 议价
    bargain = any(k in text for k in ["便宜", "优惠", "少点", "最低", "降", "折", "一口价"]) \
        or bool(re.search(r"\d+\s*(块|元)?\s*卖不卖", text)) or "不卖我走" in text
    if bargain and not text.startswith("上新"):
        deep = re.search(r"[1-8一二三四五六七八]\s*折", text)
        big_order = re.search(r"[两二三四五六七八九]?\s*万|长期单", text)
        if deep or big_order:
            add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"超权议价：{text[:40]}", 0, "转人工"); save(d)
            return OutboundReply(text="这个量级和折扣超出我的授权了🙏 已经把您的需求原文转给老板本人跟进，附了完整聊天记录——大单老板一定亲自谈，稍等回复您。")
        if qty and qty >= 100:
            base = item["trade"] if item else None
            if base:
                p = round(base * 0.95, 1)
                desc = f"{item['name']} ×{qty} @¥{p}（百件价, 拿货95折）"
                pid = add_pending(d, uid, d["regulars"].get(uid, "顾客"), desc, p * qty, "订单核准"); save(d)
                return OutboundReply(text=f"{qty}件的量给您授权内最优：拿货价95折 ¥{p}/件，合计 ¥{p*qty:,.0f}。可以的话发付款截图，单号#{pid}老板核准即发。")
            return OutboundReply(text=f"{qty}件的量我授权内可以给到拿货价95折（这是底线价啦😄）。您说下具体货号，我直接按95折给您算总价。")
        if re.search(r"\d+\s*(块|元)?\s*卖不卖", text) or "不卖我走" in text or (item and not qty):
            if item and not qty:
                add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"议价（待量）：{text[:40]}", 0, "转人工"); save(d)
                return OutboundReply(text=f"这个价已经很实在啦😊 量大我才好申请：100件以上可到拿货价95折；再低要老板批，我已帮您递话过去。您打算拿多少？")
            return OutboundReply(text="咱家明码实价哈😊 单件不好再让，不过多件有优惠：50件到拿货价、100件再95折；您也可以关注店里满减活动。诚心要我帮您算个最合适的组合～")
        add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"议价：{text[:40]}", 0, "转人工"); save(d)
        return OutboundReply(text="价格已经很实了😊 100件以上我能给到授权优惠；再低需要老板点头，已帮您转过去，稍等～")

    # 下单
    if item and qty:
        stock = _stock_of(d, item["sku"])
        moq = item.get("moq", MOQ)
        if stock <= 0:
            d["waitlist"].append({"userid": uid, "sku": item["sku"], "ts": now_str()}); save(d)
            return OutboundReply(text=f"跟您说实话，{item['name']} 现在断货了🙏 我先帮您登记 {qty} 件的到货提醒（要取消随时说），补货第一时间通知您，绝不让您白等。")
        if qty > stock:
            return OutboundReply(text=f"如实说：{item['name']} 现货只有 {stock} 件，您要 {qty} 件——可以先发 {stock} 件，余量到货马上补发；或者看下同类现货款，我帮您配。")
        if qty < moq and ("批发" in text or "拿货" in text):
            return OutboundReply(text=f"批发拿货价要 {moq} 件起批哈～{qty} 件的话按零售价 ¥{item['retail']:g}/件 = ¥{item['retail']*qty:,.0f}。要不凑到 {moq} 件？直接降到 ¥{item['trade']:g}/件，更划算。")
        price, tag = price_for(uid, item, d)
        if qty >= moq:
            price, tag = item["trade"], "拿货价"
        total = price * qty
        desc = f"{item['name']} ×{qty} @¥{price:g}（{tag}）"
        pid = add_pending(d, uid, d["regulars"].get(uid, "顾客"), desc, total, "订单核准"); save(d)
        return OutboundReply(text=f"好的！{desc}，合计 ¥{total:,.0f}。请发付款截图，老板核准后发货（单号 #{pid}）。")

    # 报价
    if item:
        stock = _stock_of(d, item["sku"])
        who = d["regulars"].get(uid)
        moq = item.get("moq", MOQ)
        if stock <= 0:
            return OutboundReply(text=f"{item['name']} 目前断货🙏 需要的话我帮您登记，到货第一时间通知（回复「{item['sku']}来N件」即可登记）。")
        if who:
            return OutboundReply(text=f"{who}，{item['name']}您的拿货价 ¥{item['trade']:g}/件，{_fmt_stock_tail(stock)}。要多少直接说～")
        return OutboundReply(text=f"{item['name']}：零售 ¥{item['retail']:g}/件；{moq}件起批发拿货价 ¥{item['trade']:g}/件。{_fmt_stock_tail(stock)}。")

    # 目录
    if any(k in text for k in ["有什么", "目录", "有哪些", "价目"]):
        lines = [f'{c["sku"]} {c["name"]} 零售¥{c["retail"]:g}/拿货¥{c["trade"]:g}' for c in get_catalog(d)[:6]]
        return OutboundReply(text="在售主打款：\n" + "\n".join(lines) + "\n……报 SKU 或名字即可询价。")

    if "价" in text or "多少" in text:
        return OutboundReply(text="您问的这款我确认下货号🤔 方便说下 SKU（如 A3）或商品名吗？发图也行。")

    # 闲聊兜底: 友好+拉回业务
    return OutboundReply(text="哈哈谢谢关心，小力今天电量满格😄 您来了想看点什么货？报 SKU 或名字（比如「A3保温壶什么价」），下单直接说数量～")


# ── 老板端控制台 ────────────────────────────────────────────
router = APIRouter()


def _auth(request: Request):
    return request.query_params.get("key", "") == BOSS_KEY


BOSS_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>店小力 · 老板端</title><style>
:root{--b:#1F3864;--o:#F5A623;--g:#2E7D4F;--bg:#F4F6FA;--card:#fff}
*{box-sizing:border-box;margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
body{background:var(--bg);color:#1F2733;padding-bottom:40px}
header{background:var(--b);color:#fff;padding:14px 16px;display:flex;justify-content:space-between;align-items:center}
header h1{font-size:17px;font-weight:600}
.badge{background:var(--o);color:#1F2733;border-radius:12px;padding:2px 10px;font-size:13px;font-weight:600}
main{max-width:560px;margin:0 auto;padding:12px}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.tile{background:var(--card);border-radius:10px;padding:10px;text-align:center;box-shadow:0 1px 3px rgba(31,56,100,.08)}
.tile b{display:block;font-size:20px;color:var(--b)}
.tile span{font-size:12px;color:#5A6B84}
section{background:var(--card);border-radius:10px;padding:12px;margin-bottom:12px;box-shadow:0 1px 3px rgba(31,56,100,.08)}
h2{font-size:15px;color:var(--b);margin-bottom:8px}
.pend{border-left:3px solid var(--o);padding:8px;margin-bottom:8px;background:#FFFBF2;border-radius:6px;font-size:14px}
.pend small{color:#8A93A6;display:block;margin-top:2px}
.row{display:flex;gap:8px;margin-top:6px}
button{border:0;border-radius:8px;padding:8px 14px;font-size:14px;cursor:pointer}
.ok{background:var(--g);color:#fff}.no{background:#E8EBF1;color:#444}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:6px 4px;border-bottom:1px solid #EEF1F6;text-align:left}
.low{color:#C0392B;font-weight:600}
input[type=number]{width:64px;padding:4px;border:1px solid #D5DBE7;border-radius:6px}
.stockbtn{padding:4px 10px;background:var(--b);color:#fff}
pre{white-space:pre-wrap;font-size:14px;line-height:1.7;font-family:inherit}
.switch{display:flex;align-items:center;justify-content:space-between}
.toggle{background:var(--g);color:#fff;min-width:96px}
.toggle.off{background:#B03A2E}
.empty{color:#8A93A6;font-size:14px;text-align:center;padding:8px}
</style></head><body>
<header><h1>🏪 店小力 · 老板端</h1><span class="badge" id="aiBadge">AI 接待中</span></header>
<main>
<div class="tiles">
 <div class="tile"><b id="tOrders">-</b><span>今日成交</span></div>
 <div class="tile"><b id="tGmv">-</b><span>金额¥</span></div>
 <div class="tile"><b id="tProfit">-</b><span>毛利¥</span></div>
</div>
<section><h2>🟠 待核准 <span id="pCount"></span></h2><div id="pending"></div></section>
<section><h2>📦 库存（≤80 标红）</h2><table id="stock"></table></section>
<section class="switch"><h2 style="margin:0">🤖 AI 话术开关</h2>
 <button class="toggle" id="aiBtn" onclick="toggleAI()">载入中</button></section>
<section><h2>🌙 今日日报</h2><pre id="report">载入中…</pre></section>
</main>
<script>
const KEY=new URLSearchParams(location.search).get('key')||'';
async function api(p,body){const r=await fetch(p+'?key='+KEY,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return (s||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}
async function refresh(){
 const s=await api('/boss/state');
 document.getElementById('tOrders').textContent=s.today_orders;
 document.getElementById('tGmv').textContent=s.today_gmv.toLocaleString();
 document.getElementById('tProfit').textContent=s.today_profit.toLocaleString();
 document.getElementById('pCount').textContent=s.pending.length?('('+s.pending.length+')'):'';
 document.getElementById('pending').innerHTML=s.pending.length?s.pending.map(p=>
  `<div class="pend">#${p.id} ${esc(p.kind)} ｜ ${esc(p.name)}<br>${esc(p.desc)}${p.amount?` ｜ ¥${p.amount.toLocaleString()}`:''}
   <small>${p.ts}</small><div class="row">
   <button class="ok" onclick="act(${p.id},'approve')">✅ 核准</button>
   <button class="no" onclick="act(${p.id},'reject')">驳回</button></div></div>`).join('')
  :'<div class="empty">没有待办，喝口茶 ☕</div>';
 document.getElementById('stock').innerHTML='<tr><th>SKU</th><th>商品</th><th>现货</th><th>调整</th></tr>'+
  s.stock.map(c=>`<tr><td>${c.sku}</td><td>${esc(c.name)}</td>
   <td class="${c.stock<=80?'low':''}">${c.stock}</td>
   <td><input type="number" id="n_${c.sku}" value="${c.stock}">
   <button class="stockbtn" onclick="setStock('${c.sku}')">改</button></td></tr>`).join('');
 const on=s.ai_on;document.getElementById('aiBtn').textContent=on?'✅ 开启中':'🔕 已关闭';
 document.getElementById('aiBtn').className='toggle'+(on?'':' off');
 document.getElementById('aiBadge').textContent=on?'AI 接待中':'AI 已关闭';
 document.getElementById('report').textContent=s.report;
}
async function act(id,op){await api('/boss/act',{id,op});refresh()}
async function setStock(sku){const v=parseInt(document.getElementById('n_'+sku).value||'0');await api('/boss/stock',{sku,stock:v});refresh()}
async function toggleAI(){await api('/boss/toggle',{});refresh()}
refresh();setInterval(refresh,8000);
</script></body></html>"""


@router.get("/boss", response_class=HTMLResponse)
def boss_page(request: Request):
    if not _auth(request):
        return HTMLResponse("<h3 style='font-family:sans-serif'>🔒 无权访问：URL 需带 ?key=你的BOSS_KEY</h3>", status_code=401)
    return HTMLResponse(BOSS_HTML)


@router.get("/boss/state")
def boss_state(request: Request):
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    d = load()
    today = today_str()
    orders = [o for o in d["orders"] if o["ts"].startswith(today)]
    stock_view = [{"sku": c["sku"], "name": c["name"], "stock": d["stock"].get(c["sku"], 0)} for c in CATALOG]
    return {"ai_on": d.get("ai_on", True), "pending": d["pending"],
            "today_orders": len(orders),
            "today_gmv": round(sum(o["amount"] for o in orders)),
            "today_profit": round(sum(o["amount"] - o.get("cost", 0) for o in orders)),
            "stock": stock_view, "report": boss_report(d)}


@router.post("/boss/act")
async def boss_act(request: Request):
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    body = await request.json()
    pid, op = body.get("id"), body.get("op")
    d = load()
    p = next((x for x in d["pending"] if x["id"] == pid), None)
    if p:
        d["pending"] = [x for x in d["pending"] if x["id"] != pid]
        if op == "approve" and p["kind"] == "订单核准":
            m = re.search(r"([A-Z]\d).*×(\d+)", p["desc"])
            cost = 0
            if m:
                sku, q = m.group(1), int(m.group(2))
                d["stock"][sku] = max(0, d["stock"].get(sku, 0) - q)
                item = next((c for c in CATALOG if c["sku"] == sku), None)
                cost = (item["trade"] * 0.8) * q if item else 0  # 演示成本口径
            d["orders"].append({"id": p["id"], "userid": p["userid"], "desc": p["desc"],
                                "amount": p["amount"], "cost": round(cost, 1), "ts": now_str()})
        save(d)
    return {"ok": True}


@router.post("/boss/stock")
async def boss_stock(request: Request):
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    body = await request.json()
    d = load(); d["stock"][body["sku"]] = max(0, int(body["stock"])); save(d)
    return {"ok": True}


@router.post("/boss/toggle")
async def boss_toggle(request: Request):
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    d = load(); d["ai_on"] = not d.get("ai_on", True); save(d)
    return {"ok": True, "ai_on": d["ai_on"]}


@router.get("/status")
def status():
    envs = {k: ("✓ 已配置" if os.environ.get(k) else "✗ 缺失")
            for k in ["WECOM_CORP_ID", "WECOM_KF_SECRET", "WECOM_TOKEN", "WECOM_AES_KEY", "BOSS_KEY"]}
    d = load()
    return {"service": "店小力 AI 店员", "env": envs, "ai_on": d.get("ai_on", True),
            "pending": len(d["pending"]), "orders_total": len(d["orders"]),
            "last_error": LAST_ERROR if LAST_ERROR["text"] else "无",
            "提示": "出错时这里会给出人话修复建议; /boss?key=BOSS_KEY 打开老板端; /egress 查出口IP"}
