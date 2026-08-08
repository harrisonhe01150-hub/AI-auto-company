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
]
MOQ = 50  # 拿货价起批量


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
def find_item(text):
    for c in CATALOG:
        if c["sku"].lower() in text.lower() or any(
            k in text for k in [c["name"], c["name"][2:5]]
        ) or (len(c["name"]) > 4 and c["name"][2:] [:2] in text):
            return c
    # 关键词模糊
    kw = {"保温壶": "A3", "玻璃杯": "B1", "吸管": "C2", "数据线": "D5", "袜": "E8",
          "伞": "F1", "帆布": "G6", "购物袋": "G6", "马克杯": "H2", "台灯": "J9",
          "音箱": "K4", "保鲜盒": "L7", "毛巾": "M3"}
    for k, sku in kw.items():
        if k in text:
            return next(c for c in CATALOG if c["sku"] == sku)
    return None


def parse_qty(text):
    m = re.search(r"(\d+)\s*[个件套条打双箱]", text)
    return int(m.group(1)) if m else None


def price_for(userid, item, d):
    if userid in d["regulars"]:
        return item["trade"], "拿货价"
    return item["retail"], "零售价"


def add_pending(d, userid, name, desc, amount, kind):
    pid = d["seq"]; d["seq"] += 1
    d["pending"].append({"id": pid, "userid": userid, "name": name,
                         "desc": desc, "amount": amount, "kind": kind, "ts": now_str()})
    return pid


def boss_report(d):
    today = today_str()
    orders = [o for o in d["orders"] if o["ts"].startswith(today)]
    gmv = sum(o["amount"] for o in orders)
    profit = sum(o["amount"] - o.get("cost", 0) for o in orders)
    low = [f'{c["name"]}×{d["stock"].get(c["sku"], 0)}' for c in CATALOG
           if 0 < d["stock"].get(c["sku"], 0) <= 80]
    out = [f'{c["name"]}' for c in CATALOG if d["stock"].get(c["sku"], 0) == 0]
    lines = [f"📊 今日经营（{today}）",
             f"成交 {len(orders)} 单 ｜ 金额 ¥{gmv:,.0f} ｜ 毛利 ¥{profit:,.0f}",
             f"待核准 {len(d['pending'])} 笔"]
    if low: lines.append("⚠️ 低库存: " + "、".join(low))
    if out: lines.append("❌ 断货: " + "、".join(out) + f"（{len(d['waitlist'])} 人登记等货）")
    if out or low: lines.append("💡 建议: 优先补断货与低库存款，断货款有登记客户可定向通知到货。")
    return "\n".join(lines)


def brain(msg):
    """wecom_core.InboundMessage -> OutboundReply | None"""
    d = load()
    text, uid = (msg.text or "").strip(), msg.sender_id

    # 老板绑定与老板指令（微信端大白话）
    if text.startswith("绑定老板"):
        if BOSS_KEY and BOSS_KEY in text:
            d["boss_userid"] = uid; save(d)
            return OutboundReply(text="✅ 老板身份已绑定。可直接说：今天卖得怎么样 / 上新A3 100个 / 开启AI / 关闭AI")
        return OutboundReply(text="口令不对，格式：绑定老板 你的BOSS_KEY")
    if uid and uid == d.get("boss_userid"):
        if "卖得怎么样" in text or "日报" in text:
            return OutboundReply(text=boss_report(d))
        m = re.search(r"上新\s*([A-Za-z]\d)\s*(\d+)", text)
        if m:
            sku, n = m.group(1).upper(), int(m.group(2))
            d["stock"][sku] = d["stock"].get(sku, 0) + n; save(d)
            item = next((c for c in CATALOG if c["sku"] == sku), None)
            return OutboundReply(text=f"✅ {item['name'] if item else sku} 库存 +{n} → {d['stock'][sku]}")
        if "关闭AI" in text.replace(" ", ""):
            d["ai_on"] = False; save(d)
            return OutboundReply(text="🔕 AI 接待已关闭（顾客消息将只进工作台）")
        if "开启AI" in text.replace(" ", ""):
            d["ai_on"] = True; save(d)
            return OutboundReply(text="🔔 AI 接待已开启")

    if not d.get("ai_on", True):
        return None  # 话术开关关闭: 静默

    # 顾客进店
    if text == "__EVENT_ENTER_SESSION__":
        who = d["regulars"].get(uid)
        hello = f"{who}，欢迎回来！" if who else "您好，我是店小力的AI店员小力～"
        return OutboundReply(text=hello + "本店批发零售双价，50件起享拿货价。想看什么直接说，比如「A3保温壶什么价」。")

    # 付款截图 → OCR 入口 → 待核准
    if msg.msg_type == "image":
        add_pending(d, uid, d["regulars"].get(uid, "顾客"),
                    "付款截图（金额待核对）", 0, "付款核验"); save(d)
        return OutboundReply(text="收到您的付款截图📷 正在核对金额，老板核准后马上安排发货～")

    # 转人工/投诉
    if any(k in text for k in ["人工", "投诉", "找老板", "转老板"]):
        add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"顾客请求人工：{text[:40]}", 0, "转人工"); save(d)
        return OutboundReply(text="好的，已把完整对话转给老板本人，马上回复您🙏")

    item = find_item(text)
    qty = parse_qty(text)

    # 议价
    if item and any(k in text for k in ["便宜", "优惠", "少点", "最低", "降"]):
        if qty and qty >= 100:
            p = round(item["trade"] * 0.95, 1)
            desc = f"{item['name']} ×{qty} @¥{p}（百件价, 拿货95折）"
            pid = add_pending(d, uid, d["regulars"].get(uid, "顾客"), desc, p * qty, "订单核准"); save(d)
            return OutboundReply(text=f"{qty}件的量给您百件价：¥{p}/件，合计 ¥{p*qty:,.0f}。可以的话发付款截图，单号#{pid}老板核准即发货。")
        add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"超权议价：{text[:40]}", 0, "转人工"); save(d)
        return OutboundReply(text=f"这个价已经很实了😊 100件以上我能再申请优惠；再低要老板批，已帮您转过去，稍等～")

    # 下单意向
    if item and qty:
        stock = d["stock"].get(item["sku"], 0)
        if stock <= 0:
            d["waitlist"].append({"userid": uid, "sku": item["sku"], "ts": now_str()}); save(d)
            return OutboundReply(text=f"{item['name']} 暂时断货了🙏 已帮您登记到货提醒，补货第一时间通知您。")
        if qty > stock:
            return OutboundReply(text=f"{item['name']} 现货只有 {stock} 件，您要 {qty} 件——可先发 {stock} 件，余量到货补发，可以吗？")
        price, tag = price_for(uid, item, d)
        if qty >= MOQ:
            price, tag = item["trade"], "拿货价"
        total = price * qty
        desc = f"{item['name']} ×{qty} @¥{price}（{tag}）"
        pid = add_pending(d, uid, d["regulars"].get(uid, "顾客"), desc, total, "订单核准"); save(d)
        return OutboundReply(text=f"好的！{desc}，合计 ¥{total:,.0f}。请发付款截图，老板核准后发货（单号 #{pid}）。")

    # 报价
    if item:
        stock = d["stock"].get(item["sku"], 0)
        who = d["regulars"].get(uid)
        if stock <= 0:
            return OutboundReply(text=f"{item['name']} 目前断货🙏 要的话我帮您登记，到货第一时间通知（回复「{item['sku']}来N件」即可登记）。")
        if who:
            return OutboundReply(text=f"{who}，{item['name']}您的拿货价 ¥{item['trade']}/件，现货 {stock}。要多少直接说～")
        return OutboundReply(text=f"{item['name']}：零售 ¥{item['retail']}/件；{MOQ}件起批发拿货价 ¥{item['trade']}/件。现货 {stock}。")

    # 库存询问(无具体商品) / 目录
    if any(k in text for k in ["有什么", "目录", "有哪些", "价目"]):
        lines = [f'{c["sku"]} {c["name"]} 零售¥{c["retail"]}/拿货¥{c["trade"]}' for c in CATALOG[:6]]
        return OutboundReply(text="在售主打款：\n" + "\n".join(lines) + "\n……报 SKU 或名字即可询价。")

    if "价" in text or "多少" in text:
        return OutboundReply(text="您问的这款我确认下货号🤔 方便说下 SKU（如 A3）或商品名吗？发图也行。")

    return OutboundReply(text="收到！想看货报 SKU 或名字（比如「A3保温壶什么价」），下单说数量（「A3来60个」），随时在～")


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
