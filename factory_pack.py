# -*- coding: utf-8 -*-
"""
factory_pack.py — 工厂行业包 v1（路线图序 2）

给「生产型企业 / 外贸询盘为主」的客户用。全部确定性逻辑：报价由代码算，AI 只负责措辞。
挂在 dianxiaoli_core 上，通过 d["factory"]["enabled"] 开关；不开时对现有档口逻辑零影响。

能力：
  2.1 FOB 阶梯报价矩阵：数量档 × 港口 × 币种；每 SKU 可自带 fob_tiers，没有就按拿货价推
  2.2 MOQ / 打样 / 交期：起订量判定、样品单流程（样品费 → 待办「样品单」）、交期按数量档
  2.4 报价单（PI 草稿）：一条指令生成结构化报价单并进老板待办「报价单」
  2.5 发货单号推送：老板「17 发货 SF1234567」→ 台账记单号 + 自动推给买家；买家问 tracking 直接答
  2.3 规格图分流由 dianxiaoli_brain.VISION_PROMPT + core 图片分支承接（kind == "spec" → 待办「转工程」）
"""
import re

DEFAULTS = {
    "enabled": False,
    "ports": ["Ningbo", "Yiwu", "Shanghai", "Shenzhen"],
    "default_port": "Ningbo",
    "currency": "USD",
    "fx": 7.2,                 # CNY per USD
    "fob_uplift": 1.22,        # 拿货价 → FOB：出口包装 + 港杂 + 利润
    "moq_fob": 500,
    "tier_discounts": [[2000, 0.98], [5000, 0.95], [10000, 0.92]],   # 数量 ≥ N → 系数
    "sample_fee_usd": 30,
    "sample_lead_days": 7,
    "lead_time": [[1000, 15], [5000, 25], [None, 40]],               # 数量 ≤ N → 天数
    "eng_reply_hours": 24,
    "contact": "",             # 工程/业务联系人，转工程时给买家
}

CARRIERS = [("SF", "顺丰 SF Express"), ("YT", "圆通 YTO"), ("ZTO", "中通 ZTO"), ("JD", "京东物流"),
            ("JT", "极兔 J&T"), ("YD", "韵达"), ("STO", "申通"), ("EMS", "EMS"), ("DHL", "DHL"),
            ("UPS", "UPS"), ("FDX", "FedEx"), ("1Z", "UPS"), ("MSK", "Maersk"), ("COSU", "COSCO")]

_QTY = re.compile(r"(\d[\d,]*)\s*(?:pcs?|pieces?|units?|sets?|个|件|套|条|双|箱|只)", re.I)
_QTY_LOOSE = re.compile(r"\b(\d{2,7})\b")
_PORT = re.compile(r"\b(ningbo|yiwu|shanghai|shenzhen|guangzhou|qingdao|xiamen|tianjin)\b|(宁波|义乌|上海|深圳|广州|青岛|厦门|天津)", re.I)
_PORT_CN = {"宁波": "Ningbo", "义乌": "Yiwu", "上海": "Shanghai", "深圳": "Shenzhen", "广州": "Guangzhou",
            "青岛": "Qingdao", "厦门": "Xiamen", "天津": "Tianjin"}

KW_FOB = ("fob", "exw", "cif", "export price", "factory price", "出口价", "离岸价", "fob价", "外贸价")
KW_SAMPLE = ("sample", "samples", "打样", "样品", "寄样", "看样")
KW_LEAD = ("lead time", "leadtime", "delivery time", "how long", "how many days", "production time",
           "交期", "多久能出", "多少天", "什么时候能发", "生产周期", "货期")
KW_QUOTE = ("quotation", "proforma", "pi ", " pi", "invoice", "报价单", "形式发票", "quote sheet", "send me a quote")
KW_TRACK = ("tracking", "track", "shipped", "shipment", "waybill", "awb", "发货了吗", "发了吗", "单号", "物流", "发了没", "到哪了", "什么时候发货", "发货了没", "发出了吗")
KW_ORDER = ("order", "confirm", "place", "下单", "要了", "确认")


def cfg(d):
    c = dict(DEFAULTS)
    c.update(d.get("factory") or {})
    return c


def enabled(d):
    return bool((d.get("factory") or {}).get("enabled"))


# ─────────────────────────────────────────────── 解析
def parse_qty(text):
    m = _QTY.search(text.replace(",", ""))
    if m:
        return int(m.group(1).replace(",", ""))
    m = _QTY_LOOSE.search(text)
    return int(m.group(1)) if m else 0


def parse_port(text, c):
    m = _PORT.search(text)
    if not m:
        return c["default_port"]
    if m.group(1):
        return m.group(1).capitalize()
    return _PORT_CN.get(m.group(2), c["default_port"])


def intent(text):
    t = (text or "").lower()
    if any(k in t for k in KW_TRACK):
        return "tracking"
    if any(k in t for k in KW_SAMPLE):
        return "sample"
    if any(k in t for k in KW_QUOTE):
        return "quotation"
    if any(k in t for k in KW_LEAD):
        return "lead"
    if any(k in t for k in KW_FOB):
        return "fob"
    return None


def _is_en(text):
    letters = len(re.findall(r"[A-Za-z]", text or ""))
    return letters >= 6 and letters >= len(text or "") * 0.3


# ─────────────────────────────────────────────── 计算（纯代码）
def fob_unit_usd(item, qty, c):
    """单价：优先 SKU 自带 fob_tiers（[{min, usd}]），否则拿货价推算 × 数量档系数。"""
    tiers = item.get("fob_tiers")
    if tiers:
        best = None
        for t in sorted(tiers, key=lambda x: x["min"]):
            if qty >= t["min"]:
                best = t
        if best is None:
            best = sorted(tiers, key=lambda x: x["min"])[0]
        return round(float(best["usd"]), 2), f"{best['min']}+ pcs"
    base = float(item["trade"]) / float(c["fx"]) * float(c["fob_uplift"])
    factor, label = 1.0, f"{c['moq_fob']}+ pcs"
    for n, f in c["tier_discounts"]:
        if qty >= n:
            factor, label = f, f"{n}+ pcs"
    return round(base * factor, 2), label


def lead_days(qty, c):
    for cap, days in c["lead_time"]:
        if cap is None or qty <= cap:
            return days
    return c["lead_time"][-1][1]


def moq_of(item, c):
    return int(item.get("moq_fob") or c["moq_fob"])


def quote(item, qty, port, c):
    unit, label = fob_unit_usd(item, qty, c)
    moq = moq_of(item, c)
    return {"sku": item["sku"], "name": item["name"], "qty": qty, "port": port,
            "unit_usd": unit, "tier": label, "total_usd": round(unit * qty, 2),
            "moq": moq, "lead_days": lead_days(max(qty, moq), c), "currency": c["currency"]}


def carrier_of(tracking):
    up = (tracking or "").upper()
    for pre, name in CARRIERS:
        if up.startswith(pre):
            return name
    return "物流"


# ─────────────────────────────────────────────── 买家侧
def handle_buyer(text, item, uid, d, core, kfid=""):
    """返回 OutboundReply 或 None（None = 交回原有逻辑）。只在 factory.enabled 时被调用。"""
    c = cfg(d)
    it = intent(text)
    en = _is_en(text)
    OR = core.OutboundReply

    # ── 物流查询：从台账答，不猜 ──
    if it == "tracking":
        mine = [o for o in d.get("orders", []) if o.get("userid") == uid]
        shipped = [o for o in mine if o.get("tracking")]
        if shipped:
            o = shipped[-1]
            if en:
                return OR(text=f"Order #{o['id']} has shipped via {o.get('carrier', 'courier')}, tracking no. {o['tracking']}. "
                               f"Let me know if you need the packing list.")
            return OR(text=f"您的单 #{o['id']} 已发出，{o.get('carrier', '物流')}单号 {o['tracking']}。需要装箱单说一声。")
        if mine:
            if en:
                return OR(text=f"Order #{mine[-1]['id']} is confirmed and in production; not shipped yet. "
                               f"I'll send you the tracking number the moment it leaves the factory.")
            return OR(text=f"您的单 #{mine[-1]['id']} 已确认、在生产中，还没发出。一发出我马上把单号发您。")
        return OR(text="I don't see a confirmed order under this chat yet — tell me the item and quantity and I'll quote right away."
                  if en else "这个会话下还没有已确认的订单。您说下款和数量，我马上报价。")

    if it is None:
        return None

    qty = parse_qty(text) or int(core._mem(d, uid).get("last_qty") or 0)   # 追问时沿用刚聊的量
    port = parse_port(text, c)

    # ── 打样 ──
    if it == "sample":
        if not item:
            return OR(text=(f"Sure, we do samples. Sample fee US${c['sample_fee_usd']}/pc (deducted from your bulk order), "
                            f"ready in {c['sample_lead_days']} days, courier at your cost. Which item?")
                      if en else
                      (f"可以打样。样品费 {c['sample_fee_usd']} 美元/件（大货下单后抵扣），{c['sample_lead_days']} 天出样，"
                       f"快递费到付。您要哪款？"))
        pid = core.add_pending(d, uid, d.get("regulars", {}).get(uid, "买家"),
                               f"样品单：{item['name']} ×1，样品费 US${c['sample_fee_usd']}", 0, "样品单", kfid=kfid)
        core.save(d)
        if en:
            return OR(text=(f"Sample of {item['name']} ({item['sku']}): fee US${c['sample_fee_usd']} (credited back on bulk order), "
                            f"ready in {c['sample_lead_days']} days, shipped by courier at your cost. "
                            f"Sample request #{pid} is logged — please send your courier account or address and I'll confirm."))
        return OR(text=(f"{item['name']}（{item['sku']}）打样：样品费 {c['sample_fee_usd']} 美元（大货抵扣），"
                        f"{c['sample_lead_days']} 天出样，快递到付。样品单 #{pid} 已登记，您把收件地址发我，老板确认后安排。"))

    # ── 交期 ──
    if it == "lead":
        q = qty or (item and moq_of(item, c)) or c["moq_fob"]
        days = lead_days(q, c)
        if en:
            return OR(text=(f"Lead time for {q:,} pcs is about {days} days after deposit and sample/artwork confirmation"
                            f"{' for ' + item['name'] if item else ''}. Larger runs are quoted case by case."))
        return OR(text=f"{q:,} 件{('的 ' + item['name']) if item else ''}交期约 {days} 天（定金和样品/图稿确认后起算），更大的量单独排。")

    # ── FOB 报价 / 报价单 ──
    if not item:
        return OR(text=(f"Sure — tell me the item (SKU or name), quantity and port, and I'll quote FOB "
                        f"{c['default_port']} right away. MOQ {c['moq_fob']:,} pcs for FOB terms.")
                  if en else f"可以，您说下款号、数量和港口，我马上报 FOB {c['default_port']} 价。FOB 起订 {c['moq_fob']:,} 件。")
    moq = moq_of(item, c)
    if qty and qty < moq:
        u, _ = fob_unit_usd(item, moq, c)
        if en:
            return OR(text=(f"MOQ for {item['name']} on FOB terms is {moq:,} pcs (US${u}/pc at MOQ, FOB {port}). "
                            f"{qty:,} pcs is below MOQ — I can do a sample order first, or quote {moq:,} pcs. Which works?"))
        return OR(text=f"{item['name']} FOB 起订 {moq:,} 件（起订价 US${u}/件，FOB {port}）。{qty:,} 件不够起订量，可以先走样品单，或者按 {moq:,} 件报。您看哪种？")
    q = qty or moq
    qt = quote(item, q, port, c)
    if it == "quotation":
        desc = f"报价单：{qt['name']} ×{qt['qty']:,} @US${qt['unit_usd']} FOB {qt['port']} = US${qt['total_usd']:,.2f}，交期 {qt['lead_days']} 天"
        pid = core.add_pending(d, uid, d.get("regulars", {}).get(uid, "买家"), desc, 0, "报价单", kfid=kfid)
        core.save(d)
        if en:
            return OR(text=(f"Quotation #{pid}\n{qt['name']} ({qt['sku']}) × {qt['qty']:,} pcs\n"
                            f"Unit: US${qt['unit_usd']} FOB {qt['port']} ({qt['tier']})\nTotal: US${qt['total_usd']:,.2f}\n"
                            f"MOQ {qt['moq']:,} · Lead time ~{qt['lead_days']} days · Terms: 30% deposit, balance before shipment\n"
                            f"Valid 15 days. The PI will be confirmed by our manager and sent to you shortly."))
        return OR(text=(f"报价单 #{pid}\n{qt['name']}（{qt['sku']}）× {qt['qty']:,} 件\n单价 US${qt['unit_usd']} FOB {qt['port']}（{qt['tier']}）\n"
                        f"合计 US${qt['total_usd']:,.2f}\n起订 {qt['moq']:,} · 交期约 {qt['lead_days']} 天 · 30% 定金，发货前付清\n"
                        f"有效期 15 天。正式 PI 老板确认后发您。"))
    core._remember(d, uid, item, q)
    core.save(d)
    if en:
        return OR(text=(f"FOB quote for {qt['name']} ({qt['sku']}), {qt['qty']:,} pcs: US${qt['unit_usd']}/pc FOB {qt['port']} "
                        f"({qt['tier']}), total US${qt['total_usd']:,.2f}. Includes export packing & port charges. "
                        f"MOQ {qt['moq']:,} pcs · lead time ~{qt['lead_days']} days. Say the word and I'll issue a PI. 也可以中文聊～"))
    return OR(text=(f"{qt['name']}（{qt['sku']}）{qt['qty']:,} 件 FOB {qt['port']}：US${qt['unit_usd']}/件（{qt['tier']}），"
                    f"合计 US${qt['total_usd']:,.2f}，含出口包装和港杂。起订 {qt['moq']:,} 件，交期约 {qt['lead_days']} 天。要报价单说一声。"))


# ─────────────────────────────────────────────── 老板侧：发货单号
_SHIP = re.compile(r"(?:#?\s*(\d+)\s*(?:发货|已发|shipped?)\s*[:：]?\s*([A-Za-z0-9\-]{6,}))|(?:(?:发货|shipped?)\s*#?\s*(\d+)\s*[:：]?\s*([A-Za-z0-9\-]{6,}))")


def owner_ship(text, d, core):
    """「17 发货 SF1234567」/「发货 17 SF1234567」→ 台账记单号 + 回告买家。返回 OutboundReply 或 None。"""
    m = _SHIP.search(text or "")
    if not m:
        return None
    oid = int(m.group(1) or m.group(3))
    tracking = (m.group(2) or m.group(4)).strip().upper()
    order = next((o for o in d.get("orders", []) if o.get("id") == oid), None)
    if not order:
        return core.OutboundReply(text=f"没找到已核准的单 #{oid}。发货单号只能挂在核准过的订单上，回「待办」看看它是不是还没核准。")
    carrier = carrier_of(tracking)
    order["tracking"] = tracking
    order["carrier"] = carrier
    order["shipped_ts"] = core.now_str()
    en = bool(order.get("lang") == "en")
    msg = (f"Order #{oid} has shipped via {carrier}, tracking no. {tracking}. Thank you!" if en
           else f"您的单 #{oid} 已发出，{carrier}单号 {tracking}，注意查收。")
    ok = core._notify({"userid": order.get("userid", ""), "kfid": order.get("kfid", ""), "id": oid,
                       "name": order.get("name", "买家")}, msg, d)
    core.save(d)
    return core.OutboundReply(text=f"✅ 单 #{oid} 已记发货：{carrier} {tracking}。" + ("买家已收到单号。" if ok else "买家通知没发出去（看 /status 的 notify）。"))
