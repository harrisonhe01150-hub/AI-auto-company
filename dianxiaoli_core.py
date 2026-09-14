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
import conv_log as _cl

CN_TZ = timezone(timedelta(hours=8))
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))   # 配 Railway Volume 时设为 /data，重启不丢
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path(".")
DATA_PATH = DATA_DIR / "dianxiaoli_data.json"
MEDIA_DIR = DATA_DIR / "dianxiaoli_media"
_LOCK = threading.Lock()
LAST_ERROR = {"text": "", "hint": "", "ts": ""}
NOTIFIER = None          # (kfid, userid, text) -> None ; 由 wecom_service 注入
NOTIFY_LOG = []          # 最近通知留痕（老板端/自检可见）

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
        "demo_catalog": True,    # 内置演示商品是否对买家显示（真实客户店可一句话关掉）
        "boss_userid": "",
        "regulars": {},          # userid -> 称呼(熟客享拿货价)
        "stock": {c["sku"]: c["stock"] for c in CATALOG},
        "pending": [],           # 待核准 {id,userid,name,desc,amount,kind,ts}
        "orders": [],            # 已核准成交 {id,userid,desc,amount,cost,ts}
        "waitlist": [],          # 断货登记 {userid,sku,ts}
        "customers": {},         # 新客画像建档
        "custom_skus": [],       # 老板上新的SKU
        "sessions": {},          # 会话记忆 userid -> {last_sku,last_qty,ts}
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


_cl.set_error_hook(record_error)


# ── 销售大脑（确定性演示版；生产版换 RAG+LLM 入口）──────────
def demo_on(d):
    """内置演示商品是否显示。旧存档没这个键时按「显示」处理（向后兼容）。"""
    return bool((d or {}).get("demo_catalog", True))


def get_catalog(d):
    """基础目录 + 老板自定义（同 SKU 时自定义覆盖基础，新 SKU 追加在后）

    老板关掉演示商品后，店里只剩他自己上新/导表的商品（顺序保持不变）。
    """
    if not demo_on(d):
        return list(d.get("custom_skus", []))
    custom = {c["sku"]: c for c in d.get("custom_skus", [])}
    merged = [custom.pop(c["sku"], c) for c in CATALOG]
    return merged + list(custom.values())


def _mem(d, uid):
    return d.setdefault("sessions", {}).setdefault(uid, {})


def _remember(d, uid, item=None, qty=None):
    s = _mem(d, uid)
    if item: s["last_sku"] = item["sku"]
    if qty: s["last_qty"] = qty
    s["ts"] = now_str()


def _recall_item(d, uid):
    sku = _mem(d, uid).get("last_sku")
    return next((c for c in get_catalog(d) if c["sku"] == sku), None) if sku else None


_ITEM_KW = {"保温壶": "A3", "茶壶": "A3", "水壶": "A3", "壶": "A3", "kettle": "A3",
          "玻璃杯": "B1", "glass cup": "B1", "吸管杯": "C2", "吸管": "C2",
          "数据线": "D5", "充电线": "D5", "线": "D5",
          "袜": "E8", "伞": "F1", "帆布": "G6", "购物袋": "G6", "环保袋": "G6",
          "马克杯": "H2", "陶瓷杯": "H2", "台灯": "J9", "灯": "J9",
          "音箱": "K4", "喇叭": "K4", "蓝牙": "K4",
          "保鲜盒": "L7", "饭盒": "L7", "便当盒": "L7",
          "毛巾": "M3", "浴巾": "M3", "裙": "D1", "连衣裙": "D1", "杯子": "B1", "水杯": "B1"}


# 问货的信号词：单字关键词（线/壶/灯/裙/袜/伞）太容易误伤，
# 「在线吗」「上线了」不是来问数据线的；带上这些词才算问货。
_SHOP_SIGNAL = ("多少", "价", "钱", "来", "要", "几", "批发", "拿货",
                "下单", "订", "买", "发", "件", "个", "有没有", "货")


def _kw_hit(k, text, low):
    """关键词命中判定。≥2 字照旧；单字得「不贴在别的汉字后面」或者整句在问货。"""
    if k.lower() not in low:
        return False
    if len(k) >= 2:
        return True
    i = text.find(k)
    if i > 0 and re.match(r"[一-鿿]", text[i - 1]):      # 在线 / 上线 / 这条裙…
        return any(s in text for s in _SHOP_SIGNAL)
    return True


def find_item(text, d=None):
    cat = get_catalog(d) if d else CATALOG
    for c in cat:
        if c["sku"].lower() in text.lower() or c["name"] in text:
            return c
    kw = _ITEM_KW
    low = text.lower()
    for k, sku in kw.items():
        if _kw_hit(k, text, low):
            hit = next((c for c in cat if c["sku"] == sku), None)
            if hit:
                return hit
    # 名称中的中文片段命中（≥2 汉字，避免数字/英文误匹配）
    best, best_len = None, 0
    for c in cat:
        name = re.sub(r"[A-Za-z0-9\.\(\)（）]+", "", c["name"])  # 只留中文部分
        for n in range(len(name), 1, -1):
            for i in range(0, len(name) - n + 1):
                frag = name[i:i + n]
                if len(frag) >= 2 and re.fullmatch(r"[\u4e00-\u9fff]{2,}", frag) and frag in text:
                    if n > best_len:
                        best, best_len = c, n
                    break
    return best


_CN_NUM = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}

def _cn2int(s):
    """中文数字转阿拉伯: 三 / 十五 / 二十 / 五十 / 一百 / 两百五"""
    if s.isdigit():
        return int(s)
    total, unit_hundred = 0, False
    if "百" in s:
        a, _, b = s.partition("百")
        total += (_CN_NUM.get(a, 1) if a else 1) * 100
        s, unit_hundred = b, True
    if "十" in s:
        a, _, b = s.partition("十")
        total += (_CN_NUM.get(a, 1) if a else 1) * 10 + _CN_NUM.get(b, 0)
        return total
    if s:
        v = _CN_NUM.get(s)
        if v is None:
            return total if unit_hundred else None
        total += v * (10 if unit_hundred and len(s) == 1 and total >= 100 else 1)
    return total or None


_UNIT = r"(?:个|件|套|条|打|双|箱|只|台|支|包|袋|units?|pcs?|pieces?)"

def parse_qty(text):
    m = re.search(r"(\d+)\s*" + _UNIT, text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"([零一二两三四五六七八九十百]+)\s*" + _UNIT, text)
    if m:
        v = _cn2int(m.group(1))
        if v:
            return v
    m = re.search(r"[来要拿订下](\s*)(\d+)(?![折%])", text)
    if m:
        return int(m.group(2))
    m = re.search(r"[来要拿订下]\s*([零一二两三四五六七八九十百]+)", text)
    if m:
        return _cn2int(m.group(1))
    m = re.search(r"^\s*(\d{1,5})\s*$", text)   # 顾客只回一个数字
    return int(m.group(1)) if m else None


def price_for(userid, item, d):
    if userid in d["regulars"]:
        return item["trade"], "拿货价"
    return item["retail"], "零售价"


def _save_media(d, image_bytes, ext="jpg"):
    """把顾客发来的图片落地，返回可在老板端引用的 media_id。"""
    if not image_bytes:
        return ""
    try:
        MEDIA_DIR.mkdir(exist_ok=True)
        mid = f"m{d['seq']}_{int(len(image_bytes))}"
        (MEDIA_DIR / f"{mid}.{ext}").write_bytes(image_bytes)
        return f"{mid}.{ext}"
    except Exception as e:
        record_error(e)
        return ""


def add_pending(d, userid, name, desc, amount, kind, media="", kfid=""):
    pid = d["seq"]; d["seq"] += 1
    p = {"id": pid, "userid": userid, "name": name,
         "desc": desc, "amount": amount, "kind": kind,
         "media": media, "kfid": kfid, "ts": now_str()}
    d["pending"].append(p)
    _cl.log_pending(userid, pid, kind, desc, amount)
    # 推给老板（群机器人/微信客服）。不依赖磁盘状态——很多调用点还没 save(d)。
    try:
        import boss_notify as _bn
        _bn.push(_bn.fmt_pending(p), kind="pending", core=__import__(__name__))
    except Exception as _e:
        record_error(_e)
    return pid


def _stock_of(d, sku):
    return d["stock"].get(sku, 0)


def _fmt_stock_tail(stock):
    """顾客侧库存表述——红线：绝不透露具体数量，只给可得性。"""
    if stock <= 0:
        return "暂时没货"
    if stock <= 5:
        return "还有，不过不多了"
    return "有货"


# ── 图片理解：分类 + 两条落地路径 ──────────────────────────
VISION_MIN_CONF = float(os.environ.get("VISION_MIN_CONF", "0.6"))


def _classify_image(msg, d):
    """调视觉大脑给图片分类；无视觉能力/异常时返回 None（上层按付款凭证兜底）。"""
    try:
        import dianxiaoli_brain as _llm
        return _llm.classify_image(msg.media_bytes, __import__(__name__), d)
    except Exception as e:
        record_error(e)
        return None


def _image_payment_flow(d, uid, media_id, ocr_amount=0, kfid=""):
    """付款凭证：金额比对 → 待核准队列（图片一并带给老板端）。

    kfid 必须一路带到待办里，否则老板核准后回告顾客的消息发不出去。
    """
    kfid = kfid or _mem(d, uid).get("kfid", "")
    my_orders = [p for p in d["pending"] if p["userid"] == uid and p["kind"] == "订单核准"]
    if ocr_amount and my_orders:
        latest = my_orders[-1]
        if abs(ocr_amount - latest["amount"]) < 0.01:
            add_pending(d, uid, d["regulars"].get(uid, "顾客"),
                        f"付款截图 ¥{ocr_amount:,.0f} 与单 #{latest['id']} 金额一致", ocr_amount,
                        "付款核验", media_id, kfid); save(d)
            return OutboundReply(text=f"看到了，¥{ocr_amount:,.0f}，跟单子金额一致。老板核准了马上给您发。")
        diff = latest["amount"] - ocr_amount
        add_pending(d, uid, d["regulars"].get(uid, "顾客"),
                    f"⚠️ 金额异常: 截图¥{ocr_amount:,.0f} vs 订单¥{latest['amount']:,.0f}（差¥{diff:,.0f}）", ocr_amount,
                    "金额异常", media_id, kfid); save(d)
        return OutboundReply(text=(
            f"收到截图📷 核对到金额 ¥{ocr_amount:,.0f} 与订单 ¥{latest['amount']:,.0f} 有出入（差 ¥{diff:,.0f}），"
            "已标记给老板确认怎么处理，请稍等——发货安排以老板核准为准哈。"))
    add_pending(d, uid, d["regulars"].get(uid, "顾客"),
                "付款截图（待老板核对）", 0, "付款核验", media_id, kfid); save(d)
    return OutboundReply(text="收到，我核对下金额。老板确认了马上安排。")


def _image_product_reply(d, uid, vis, media_id=""):
    """商品照：认出货号就直接报价+可得性（数量保密），认不出就问一句。"""
    sku = (vis or {}).get("sku", "")
    it = next((c for c in get_catalog(d) if c["sku"].upper() == sku), None)
    if not it and (vis or {}).get("note"):
        it = find_item(vis["note"], d)
    if not it:
        _mem(d, uid)["pending_image"] = {"media": media_id, "ts": now_str()}; save(d)
        return OutboundReply(text="图我看到了，是这类货没错，就是型号没敢认死📷 您说下要哪款或者报个货号，我直接给您价。")
    p, label = price_for(uid, it, d)
    _remember(d, uid, it)
    tail = _fmt_stock_tail(_stock_of(d, it["sku"]))
    if tail == "暂时没货":
        return OutboundReply(text=f"这张图是 {it['name']}（{it['sku']}）。这款{tail}，要的话我给您登记，到货第一时间通知您。")
    return OutboundReply(text=(f"这张图是 {it['name']}（{it['sku']}），{label} ¥{p:g}/件，{tail}。"
                               f"{MOQ}件起走拿货价。您要多少？我看看能不能一次发齐。"))


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


def set_notifier(fn):
    """注入主动通知通道（微信客服）。未注入时通知静默跳过，不影响主流程。"""
    global NOTIFIER
    NOTIFIER = fn


def _kfid_for(item, d=None):
    """取该顾客的客服账号ID：优先待办自带，其次会话里记的最近一次。

    图片/付款类待办早期没带 kfid，会导致核准后发不出去——这里兜底找回。
    """
    k = (item.get("kfid") or "").strip()
    if k:
        return k
    try:
        dd = d if d is not None else load()
        uid = item.get("userid", "")
        k = (dd.get("sessions", {}).get(uid, {}) or {}).get("kfid", "")
        if k:
            return k
        k = next((x.get("kfid") for x in dd.get("pending", []) if x.get("userid") == uid and x.get("kfid")), "")
        return k or dd.get("last_kfid", "")      # 单客服账号店铺：最近一次即正确
    except Exception:
        return ""


def _notify(item, text, d=None):
    """核准/驳回后回告顾客。任何异常都不许影响老板端操作。"""
    if not text:
        return False
    kfid = _kfid_for(item, d)
    rec = {"to": item.get("name", "顾客"), "text": text, "ts": now_str(), "ok": False, "why": ""}
    NOTIFY_LOG.append(rec)
    del NOTIFY_LOG[:-20]
    try:
        if not NOTIFIER:
            rec["why"] = "通道未注入(NOTIFIER=None)"
            return False
        if not kfid or not item.get("userid"):
            rec["why"] = f"缺少路由信息 kfid={'有' if kfid else '无'} userid={'有' if item.get('userid') else '无'}"
            return False
        try:
            NOTIFIER(kfid, item.get("userid", ""), text)
            rec["ok"] = True
            return True
        except Exception as e:
            rec["why"] = str(e)[:120]
            record_error(e)
            return False
    finally:
        _cl.log_notify(item.get("userid", ""), item.get("id"), rec["ok"], rec["why"], text)
        # 没发出去 → 立刻告诉老板，并把原话给他，让他手动补发（企微 48 小时限制常触发这条）
        if not rec["ok"] and rec["why"]:
            try:
                import boss_notify as _bn
                _bn.push(_bn.fmt_notify_failed(item, text, rec["why"]),
                         kind="notify_failed", core=__import__(__name__))
            except Exception as _e:
                record_error(_e)


def _act_notice(item, op):
    """按待办类型生成给顾客的回话。"""
    kind, desc, amt = item.get("kind", ""), item.get("desc", ""), item.get("amount", 0)
    if op == "approve":
        if kind == "订单核准":
            return f"老板确认了！{desc}，合计 ¥{amt:,.0f}。这就给您安排发货，发出后我把单号发您。"
        if kind == "付款核验":
            return "老板核对过了，款已收到 ✅ 这就安排发货，稍后把物流信息发您。"
        if kind == "金额异常":
            return "老板看过您的付款凭证了，按实收金额给您安排，差额我们后面再对。这就发货。"
        if kind == "转人工":
            return "老板那边已经处理好了，稍后他会亲自跟您说一声。"
        return "老板已确认，这就给您安排。"
    if kind == "订单核准":
        return "不好意思，这单老板暂时接不了——可能是货期或价格的问题。我马上帮您问清楚，回头给您准信。"
    if kind == "付款核验":
        return "老板核对时这笔款没对上 🙏 麻烦您再发一次付款凭证？或者我让老板直接跟您联系。"
    if kind == "金额异常":
        return "这笔金额跟订单对不上，老板想跟您确认一下。稍等，他马上联系您。"
    return "老板看过了，这条先不处理，稍后给您说明。"


def _do_act(d, pid, op):
    """核准/驳回一笔待办; 返回被处理的条目或 None。核准订单时扣库存并写台账。"""
    p = next((x for x in d["pending"] if x["id"] == pid), None)
    if not p:
        return None
    d["pending"] = [x for x in d["pending"] if x["id"] != pid]
    if op == "approve" and p["kind"] == "订单核准":
        cost = 0
        for m in re.finditer(r"([A-Z]\d)[^×]*×(\d+)", p["desc"]):
            sku, q = m.group(1), int(m.group(2))
            d["stock"][sku] = max(0, d["stock"].get(sku, 0) - q)
            item = next((c for c in get_catalog(d) if c["sku"] == sku), None)
            if item:
                cost += item.get("cost", item["trade"] * 0.8) * q
        d["orders"].append({"id": p["id"], "userid": p["userid"], "desc": p["desc"],
                            "amount": p["amount"], "cost": round(cost, 1), "ts": now_str(),
                            "kfid": _kfid_for(p, d), "name": p.get("name", "顾客"),
                            "lang": "en" if _latin_heavy(p.get("desc", "")) else "zh"})
    _cl.log_act(p.get("userid", ""), p.get("id"), p.get("kind", ""), op, p.get("amount", 0))
    p["notified"] = _notify(p, _act_notice(p, op), d)
    return p


def _owner_brain(text, d):
    """老板端大白话指令; 返回 OutboundReply 或 None(转顾客逻辑)"""
    # 发货单号：「17 发货 SF1234567」→ 台账 + 回告买家（工厂包 2.5，档口同样受益）
    try:
        import factory_pack as _fp
        _sr = _fp.owner_ship(text, d, __import__(__name__))
        if _sr:
            return _sr
    except Exception as _e:
        record_error(_e)
    if "工厂模式" in text.replace(" ", ""):
        on = not any(k in text for k in ["关闭", "关掉", "停用", "取消"])
        d.setdefault("factory", {})["enabled"] = on; save(d)
        return OutboundReply(text=("🏭 工厂模式已开启：买家问 FOB/打样/交期/报价单/物流，按工厂规则答；规格图自动转工程。"
                                   if on else "工厂模式已关闭，按档口规则接待。"))
    # 演示商品开关：真实客户店不能拿内置的 A3 保温壶等演示品报价
    _t = text.replace(" ", "")
    if any(k in _t for k in ["演示商品", "演示目录", "示例商品"]):
        if any(k in _t for k in ["关闭", "关掉", "隐藏", "停用", "取消"]):
            d["demo_catalog"] = False; save(d)
            n = len(d.get("custom_skus", []))
            if n == 0:
                return OutboundReply(text=(
                    "✅ 演示商品已隐藏。但您还没有上传自己的商品，买家问什么都会答「没有这款」——"
                    "请先在网页控制台上传产品表，或说「上新」。"))
            return OutboundReply(text=f"✅ 演示商品已隐藏。现在买家只能看到您自己上传/上新的 {n} 款商品。")
        if any(k in _t for k in ["开启", "打开", "显示", "恢复"]):
            d["demo_catalog"] = True; save(d)
            return OutboundReply(text=(
                "✅ 演示商品已恢复显示（A3 保温壶等 14 款），适合演示，正式接待前记得再说「关闭演示商品」。"))
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

    # ── 核准 / 驳回 ──
    if any(k in text for k in ["核准", "通过", "确认单", "批准", "驳回", "拒绝"]):
        reject = any(k in text for k in ["驳回", "拒绝"])
        op = "reject" if reject else "approve"
        if any(k in text for k in ["全部", "所有", "都"]):
            done = []
            for p in list(d["pending"]):
                r = _do_act(d, p["id"], op)
                if r: done.append(f"#{r['id']}")
            save(d)
            if not done:
                return OutboundReply(text="现在没有待核准的单子，清爽 ☕")
            return OutboundReply(text=f"✅ 已{'驳回' if reject else '核准'} {len(done)} 笔：{'、'.join(done)}\n" + boss_report(d))
        mm = re.search(r"#?(\d+)", text)
        if mm:
            p = _do_act(d, int(mm.group(1)), op)
            if p:
                save(d)
                return OutboundReply(text=f"✅ 单 #{p['id']} 已{'驳回' if reject else '核准'}：{p['desc']}\n" + boss_report(d))
            return OutboundReply(text=f"没找到单号 #{mm.group(1)}，可能已处理过。回「待办」看当前列表。")
        return OutboundReply(text="要核准哪一笔？说单号（如「核准3号」）或说「全部核准」。")

    # ── 待办列表 ──
    if any(k in text for k in ["待办", "待核准", "有什么要处理", "有单吗"]):
        if not d["pending"]:
            return OutboundReply(text="没有待办，喝口茶 ☕")
        lines = [f"#{p['id']} {p['kind']}｜{p['desc']}" + (f"｜¥{p['amount']:,.0f}" if p["amount"] else "")
                 for p in d["pending"]]
        return OutboundReply(text=f"待处理 {len(lines)} 笔：\n" + "\n".join(lines) + "\n回「核准N号」或「全部核准」。")

    # ── 库存查询 ──
    if any(k in text for k in ["还有多少", "库存", "剩多少", "快没货", "缺货", "断货"]):
        it = find_item(text, d)
        if it:
            n = d["stock"].get(it["sku"], 0)
            tail = "（已断货，建议补货）" if n == 0 else ("（低库存，建议补货）" if n <= 80 else "")
            return OutboundReply(text=f"{it['name']}：现货 {n} 件{tail}")
        cat = get_catalog(d)
        low = [f"{c['name']}×{d['stock'].get(c['sku'],0)}" for c in cat if 0 < d["stock"].get(c["sku"], 0) <= 80]
        out = [c["name"] for c in cat if d["stock"].get(c["sku"], 0) == 0]
        parts = []
        if out: parts.append("❌ 断货：" + "、".join(out))
        if low: parts.append("⚠️ 低库存：" + "、".join(low))
        if not parts: parts.append("库存都很充足 👍")
        return OutboundReply(text="\n".join(parts))

    # ── 改价 ──
    mp = re.search(r"([A-Z]\d).{0,6}?(拿货价?|批发价?|零售价?|售价)?\s*(?:改成?|调成?|设为?|变成)\s*(\d+(?:\.\d+)?)", text.upper().replace("拿货", "拿货"))
    if mp is None:
        mp = re.search(r"([A-Z]\d)[^0-9]{0,10}(?:改|调|设)[^0-9]{0,4}(\d+(?:\.\d+)?)", text.upper())
        if mp:
            sku, field, val = mp.group(1), ("拿货" if "拿货" in text or "批发" in text else "零售"), float(mp.group(2))
        else:
            sku = None
    else:
        sku, field, val = mp.group(1), (mp.group(2) or ("拿货" if "拿货" in text or "批发" in text else "零售")), float(mp.group(3))
    if sku and ("改" in text or "调" in text or "设" in text):
        item = next((c for c in get_catalog(d) if c["sku"] == sku), None)
        if item:
            key = "trade" if "拿货" in field or "批发" in field else "retail"
            override = dict(item); override[key] = val
            d["custom_skus"] = [c for c in d.get("custom_skus", []) if c["sku"] != sku] + [override]
            save(d)
            label = "拿货价" if key == "trade" else "零售价"
            return OutboundReply(text=f"✅ {item['name']} {label} 改为 ¥{val:g}（原 ¥{item[key]:g}）。下一位顾客问价即按新价报。")

    # ── 帮助 ──
    if any(k in text for k in ["能做什么", "会什么", "帮助", "怎么用", "指令"]):
        try:
            import boss_notify as _bn
            _notice = ("📣 通知：新单已自动推群\n" if _bn.configured() else
                       "📣 通知：配好群机器人后新单会自动推到群里（见 docs/BOSS_NOTIFY.md）\n")
        except Exception as _e:
            record_error(_e); _notice = ""
        return OutboundReply(text=(
            "我能帮您做这些，直接说人话就行：\n"
            "📊 经营：今天卖得怎么样 / 这周什么卖得最好\n"
            "✅ 审单：待办 / 核准3号 / 全部核准 / 驳回5号\n"
            "📦 库存：A3还有多少 / 哪些快没货了 / 上新A3 100个\n"
            "💰 价格：A3拿货价改成30\n"
            "🧾 记账：刚卖了5个保温壶给老张走拿货价\n"
            "🧪 演示：关闭演示商品 / 开启演示商品\n"
            + _notice +
            "🔔 开关：关闭AI / 开启AI"))
    return None


def brain(msg):
    """wecom_core.InboundMessage -> OutboundReply | None

    分层：老板指令(确定性) → LLM 大脑(dianxiaoli_brain, 含红线/价格守卫) → 规则引擎(兜底)。
    LLM 不可用或结果不可信时静默降级，行为与纯规则版一致。

    外层只做一件事：把进话和回话写进 conv_log（Agent C 每日审计的数据源）。
    留痕失败绝不影响接待——所有写入在 conv_log 内部吞异常。
    """
    text, uid = (msg.text or "").strip(), msg.sender_id
    kf = getattr(msg, "account_id", "") or ""
    is_boss = False
    try:
        is_boss = bool(uid) and uid == (load().get("boss_userid") or "")
    except Exception:
        pass
    scenario = _cl.classify_scenario(text, msg.msg_type, is_boss)
    _cl.log_in(uid, kf, text, msg.msg_type, scenario, is_boss)

    llm_before = _llm_ok_count()
    reply = _brain_impl(msg)
    route = "boss" if is_boss else ("llm" if _llm_ok_count() > llm_before else "rule")
    _cl.log_out(uid, kf, reply.text if reply else "", scenario, route, is_boss)
    return reply


def _llm_ok_count():
    try:
        import dianxiaoli_brain as _llm
        return int(_llm.STATS.get("llm_ok", 0))
    except Exception:
        return 0


def _brain_impl(msg):
    d = load()
    text, uid = (msg.text or "").strip(), msg.sender_id

    # 新客画像建档
    if uid and uid not in d.get("customers", {}):
        d.setdefault("customers", {})[uid] = now_str(); save(d)

    # 记住该顾客走的客服账号——老板核准后要靠它把消息发回去
    _kf = getattr(msg, "account_id", "") or ""
    if _kf and (d.get("last_kfid") != _kf or (uid and _mem(d, uid).get("kfid") != _kf)):
        d["last_kfid"] = _kf
        if uid:
            _mem(d, uid)["kfid"] = _kf
        # 把历史遗留(早期没带 kfid)的待办补上，老板核准时才发得出去
        for _p in d.get("pending", []):
            if not _p.get("kfid") and _p.get("userid") == uid:
                _p["kfid"] = _kf
        save(d)

    # 老板绑定与老板指令
    if text.startswith("绑定老板"):
        if BOSS_KEY and BOSS_KEY in text:
            d["boss_userid"] = uid; save(d)
            return OutboundReply(text="✅ 老板身份已绑定。可直接说：今天卖得怎么样 / 卖得最好的是什么 / 上新A3 100个 / 刚卖了5个保温壶给老张拿货价 / 关闭AI")
        return OutboundReply(text="口令不对，格式：绑定老板 你的BOSS_KEY")
    if text.replace(" ", "") in ("解绑老板", "取消绑定", "退出老板"):
        if uid == d.get("boss_userid"):
            d["boss_userid"] = ""; save(d)
            return OutboundReply(text="已解绑老板身份，这个号现在按顾客接待。")
        return OutboundReply(text="这个号本来就不是老板号哈。")
    if uid and uid == d.get("boss_userid"):
        r = _owner_brain(text, d)
        if r:
            return r

    if not d.get("ai_on", True):
        return None

    # ── LLM 大脑（可用时优先；不可用/不可信则静默降级到下方规则引擎）──
    try:
        import dianxiaoli_brain as _llm
        if _llm.llm_available() and msg.msg_type == "text":
            got = _llm.think(msg, __import__(__name__), d, business_name="店小力")
            if got:
                reply_text = got[0] if isinstance(got, tuple) else got
                if reply_text:
                    return OutboundReply(text=reply_text)
    except Exception as _e:      # 大脑层任何异常都不许影响接待
        record_error(_e)

    # 进店欢迎
    if text == "__EVENT_ENTER_SESSION__":
        who = d["regulars"].get(uid)
        hello = f"{who}，来啦！" if who else "你好，我是店里的AI店员小力。"
        return OutboundReply(text=hello + "看点什么？直接报货号或名字都行。")

    # 图片 → 视觉分类 → 付款凭证(转老板) / 商品照(答产品) / 存疑(反问)
    if msg.msg_type == "image":
        ocr_amount = 0
        try:
            ocr_amount = float((msg.raw or {}).get("demo_ocr_amount", 0))
        except Exception:
            pass
        media_id = _save_media(d, msg.media_bytes)
        vis = _classify_image(msg, d)
        kind = (vis or {}).get("type", "")
        # 低置信度不猜：宁可多问一句，也不能把商品照当付款凭证上报老板
        if vis and float(vis.get("confidence") or 0) < VISION_MIN_CONF:
            kind = "other"

        if kind == "product":
            return _image_product_reply(d, uid, vis, media_id)
        if kind == "spec":
            # 规格图 / 图纸 / 技术参数 → 转工程评估，不报价（工厂包 2.3）
            import factory_pack as _fp
            c = _fp.cfg(d)
            add_pending(d, uid, d["regulars"].get(uid, "买家"),
                        f"规格图/图纸转工程：{(vis or {}).get('note', '')[:40]}", 0, "转工程", media_id, _kf)
            save(d)
            en = _latin_heavy((vis or {}).get("note", "")) or _mem(d, uid).get("lang") == "en"
            who = f" ({c['contact']})" if c.get("contact") else ""
            return OutboundReply(text=(
                f"Got your drawing/spec sheet. I've passed it to our engineer{who} for a feasibility and cost check — "
                f"you'll hear back within {c['eng_reply_hours']} hours. Meanwhile, what quantity are you planning?"
                if en else
                f"收到规格图/图纸📐 已转给工程{who}评估可行性和成本，{c['eng_reply_hours']} 小时内回您。您这边大概要多少量？"))
        if kind == "other":
            _mem(d, uid)["pending_image"] = {"media": media_id, "ts": now_str()}
            save(d)
            return OutboundReply(text="收到图了📷 这是付款凭证，还是想问这个货？您说一声，我马上给您办。")
        # payment；无视觉能力时也走这里，保持原有行为
        if not ocr_amount and vis and vis.get("amount"):
            try:
                ocr_amount = float(vis["amount"])
            except Exception:
                ocr_amount = 0
        return _image_payment_flow(d, uid, media_id, ocr_amount, _kf)

    # 上一张图没判准，顾客补了一句话 → 按这句话补路由
    _stashed = _mem(d, uid).get("pending_image")
    if _stashed and text:
        if any(k in text for k in ["付款", "转账", "打款", "付了", "汇款", "收款", "支付", "打过去", "已付"]):
            _mem(d, uid).pop("pending_image", None); save(d)
            return _image_payment_flow(d, uid, _stashed.get("media", ""), 0, _kf)
        if any(k in text for k in ["问货", "这个货", "什么价", "多少钱", "有没有", "这款", "这个多少"]) or find_item(text, d):
            _mem(d, uid).pop("pending_image", None); save(d)   # 交给下方常规问价流程

    # 幻觉红线: 明确SKU码但不在目录
    sku_tokens = re.findall(r"\b([A-Z]\d)\b", text.upper())
    cat_skus = {c["sku"] for c in get_catalog(d)}
    unknown = [s for s in sku_tokens if s not in cat_skus]
    if unknown and not any(s in cat_skus for s in sku_tokens):
        near = find_item(text, d)
        if near:
            rec = f"相近的有 {near['name']}（零售¥{near['retail']:g}）可以了解下～"
        else:
            # 找不到相近的，别再拿「说下商品类目」搪塞——直接把在售的摆出来（口径同买家侧「没有这款」）
            _cat = get_catalog(d)
            rec = ("目前在售：" + "、".join(c["name"] for c in _cat[:3]) + "。报货号或名字我给您报价。"
                   if _cat else "目前店里还没上架商品，老板马上补。")
        return OutboundReply(text=f"{unknown[0]} 这个号我这儿没有，不敢瞎报价。{rec}")

    # 库存数量保密红线：顾客问"还有多少/多少库存"一律不报数字
    if any(k in text for k in ["还有多少", "多少库存", "库存多少", "剩多少", "有多少货", "存货多少", "how many in stock", "stock level"]):
        it = find_item(text, d)
        if it:
            n = _stock_of(d, it["sku"])
            if n <= 0:
                return OutboundReply(text=f"{it['name']} 现在断货了，要的话我给您登记，到货就通知您。")
            return OutboundReply(text=f"{it['name']} 有货的，具体存量不方便对外说哈。您要多少？我看看能不能一次给您发齐。")
        return OutboundReply(text="具体库存数不方便对外说哈。您说要哪款、要多少，我直接告诉您能不能发齐。")

    # 客户隐私红线 (只拦"打听他人", 不拦本人自称)
    other_names = [n for _u, n in d["regulars"].items() if _u != uid]
    if any(n in text for n in other_names) and any(k in text for k in ["什么价", "订单", "拿多少", "发我"]):
        return OutboundReply(text="别人的价保密，不能说；您的价我同样不会跟别人讲。您要多少？我按量给您算。")
    if any(k in text for k in ["别人的订单", "其他客户", "她的订单", "他的订单"]):
        return OutboundReply(text="这个真不能说。不过您量够了，价格差不了。")

    # 冒充老板/套底价
    if ("你就是老板" in text or "底价" in text) and not (uid == d.get("boss_userid")):
        return OutboundReply(text="我是店里的AI店员小力，不是老板。底价得老板点头。您说个量吧，100件以上我这儿能直接给到最优；再大的量我这就去问老板。")

    # 竞对打探
    if any(k in text for k in ["哪个厂", "厂家电话", "供应商", "进货渠道", "成本价"]):
        return OutboundReply(text="这个不方便说哈。货您放心比，长期做价格好谈。")

    # 退款/投诉/人工
    if any(k in text for k in ["退", "投诉", "碎了", "坏了", "质量", "怎么搞的"]) and msg.msg_type == "text" and not text.startswith("上新"):
        if any(k in text for k in ["投诉", "碎了", "坏了", "怎么搞的", "质量"]):
            add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"投诉/售后：{text[:40]}", 0, "转人工", kfid=msg.account_id); save(d)
            return OutboundReply(text="这事是我们的问题，实在抱歉。您的订单记录我已经一并转给老板了，马上给您处理方案。")
        add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"退换请求：{text[:40]}", 0, "转人工", kfid=msg.account_id); save(d)
        return OutboundReply(text="行，退换老板亲自跟。您的订单我一并转过去了，很快回您。")
    if any(k in text for k in ["人工", "找老板", "转老板"]):
        add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"顾客请求人工：{text[:40]}", 0, "转人工", kfid=msg.account_id); save(d)
        return OutboundReply(text="好，我把聊天记录整个转给老板了，他马上回您。")

    # 配送政策
    if any(k in text for k in ["送到", "运费", "配送", "包邮", "发货到", "ship"]) and not _latin_heavy(text):
        return OutboundReply(text=f"{SHIPPING_POLICY} 您发哪儿？我给您算算。")

    # 工厂行业包（factory.enabled 时）：FOB 阶梯报价 / 打样 / 交期 / 报价单 / 物流查询
    if _latin_heavy(text):
        _mem(d, uid)["lang"] = "en"
    try:
        import factory_pack as _fp
        if _fp.enabled(d):
            _fit = find_item(text, d) or (_recall_item(d, uid) if _fp.intent(text) else None)
            _fr = _fp.handle_buyer(text, _fit, uid, d, __import__(__name__), _kf)
            if _fr:
                return _fr
    except Exception as _e:
        record_error(_e)

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
                f"Hi! {it['name']}: MOQ {MOQ} pcs at trade price ¥{it['trade']:g} (≈US${usd})/pc, in stock. "
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
        return OutboundReply(text="给您理一下：\n" + "\n".join(lines) +
                             f"\n合计 ¥{total:,.0f}，单号 #{pid}。没问题就发个付款截图，老板确认后一起发。")

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
                return OutboundReply(text=f"改好了，单 #{p['id']}：{p['desc']}，合计 ¥{p['amount']:,.0f}。没问题就发个截图。")
        return OutboundReply(text="这单我没找着记录。说下单号，或者重报个数量，我给您重开一单。")

    item = find_item(text, d)
    qty = parse_qty(text)

    # 会话记忆：没点名商品时，沿用刚才聊的那款（30 分钟内有效）
    if item is None and qty:
        item = _recall_item(d, uid)
    if item is None and any(k in text for k in ["这个", "那个", "这款", "那款", "就它", "要了", "来吧", "行"]):
        item = _recall_item(d, uid)
    if item:
        _remember(d, uid, item, qty); save(d)

    # 议价
    bargain = any(k in text for k in ["便宜", "优惠", "少点", "最低", "降", "折", "一口价"]) \
        or bool(re.search(r"\d+\s*(块|元)?\s*卖不卖", text)) or "不卖我走" in text
    if bargain and not text.startswith("上新"):
        deep = re.search(r"[1-8一二三四五六七八]\s*折", text)
        big_order = re.search(r"[两二三四五六七八九]?\s*万|长期单", text)
        if deep or big_order:
            add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"超权议价：{text[:40]}", 0, "转人工", kfid=msg.account_id); save(d)
            return OutboundReply(text="这个量和折扣超我权限了。原话我转给老板了，这种单子他一定亲自谈，稍等。")
        if item is None:
            item = _recall_item(d, uid)
        if qty and qty >= 100:
            base = item["trade"] if item else None
            if base:
                p = round(base * 0.95, 1)
                desc = f"{item['name']} ×{qty} @¥{p}（百件价, 拿货95折）"
                pid = add_pending(d, uid, d["regulars"].get(uid, "顾客"), desc, p * qty, "订单核准", kfid=msg.account_id); save(d)
                return OutboundReply(text=f"{qty}件我这儿能给到最优：¥{p}/件（拿货价95折），合计 ¥{p*qty:,.0f}。行的话发个付款截图，单号 #{pid}，老板核准就发货。")
            return OutboundReply(text=f"{qty}件我能给到拿货价95折，这是我这儿的底了。哪个货号？我按95折给您算。")
        if re.search(r"\d+\s*(块|元)?\s*卖不卖", text) or "不卖我走" in text or (item and not qty):
            if item and not qty:
                add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"议价（待量）：{text[:40]}", 0, "转人工", kfid=msg.account_id); save(d)
                return OutboundReply(text="这价已经挺实了。量大我好开口——100件以上能到拿货价95折，再低得老板批，我先递话过去了。您打算拿多少？")
            return OutboundReply(text="单件真让不了。不过量上来有优惠：50件走拿货价，100件再95折。您要是诚心要，我帮您凑个最划算的组合。")
        add_pending(d, uid, d["regulars"].get(uid, "顾客"), f"议价：{text[:40]}", 0, "转人工", kfid=msg.account_id); save(d)
        return OutboundReply(text="价格挺实了。100件以上我能让一点，再低要老板点头——已经帮您问了，稍等。")

    # 下单
    if item and qty:
        stock = _stock_of(d, item["sku"])
        moq = item.get("moq", MOQ)
        if stock <= 0:
            d["waitlist"].append({"userid": uid, "sku": item["sku"], "ts": now_str()}); save(d)
            return OutboundReply(text=f"实话说，{item['name']} 断货了。我给您登记 {qty} 件，到货第一时间叫您——不用您惦记。")
        if qty > stock:
            return OutboundReply(text=f"跟您说实话，{item['name']} 这个量我一次发不齐。可以先发一部分、余量到货补上，也可以给您配同类现货——具体能发多少我找老板确认，马上回您。")
        if qty < moq and ("批发" in text or "拿货" in text):
            return OutboundReply(text=f"拿货价 {moq} 件起批。{qty} 件按零售 ¥{item['retail']:g}/件，合计 ¥{item['retail']*qty:,.0f}。凑到 {moq} 件就降到 ¥{item['trade']:g}，划算不少。")
        price, tag = price_for(uid, item, d)
        if qty >= moq:
            price, tag = item["trade"], "拿货价"
        total = price * qty
        desc = f"{item['name']} ×{qty} @¥{price:g}（{tag}）"
        pid = add_pending(d, uid, d["regulars"].get(uid, "顾客"), desc, total, "订单核准", kfid=msg.account_id); save(d)
        return OutboundReply(text=f"好，{desc}，合计 ¥{total:,.0f}。发个付款截图我核对，老板确认就发货。单号 #{pid}。")

    # 报价
    if item:
        stock = _stock_of(d, item["sku"])
        who = d["regulars"].get(uid)
        moq = item.get("moq", MOQ)
        if stock <= 0:
            return OutboundReply(text=f"{item['name']} 现在断货。要的话我给您记上，到货就通知——说一声「{item['sku']}来N件」就行。")
        if who:
            return OutboundReply(text=f"{who}，{item['name']} 您的价 ¥{item['trade']:g}/件，{_fmt_stock_tail(stock)}。要多少？")
        return OutboundReply(text=f"{item['name']} 零售 ¥{item['retail']:g}/件，{moq}件起走拿货价 ¥{item['trade']:g}，{_fmt_stock_tail(stock)}。要几件？")

    # 目录里真没有这款：老板关掉演示商品后，别再拿「哪一款？」来搪塞买家
    # 单字关键词（线/壶/灯/裙…）不算数：「在线吗」不该被当成问货
    if item is None and (re.search(r"[A-Za-z]\d", text)
                         or any(k.lower() in text.lower() for k in _ITEM_KW if len(k) >= 2)):
        cat = get_catalog(d)
        if not cat:
            return OutboundReply(text="这款我们这里没有，目前店里还没上架商品，老板马上补。")
        return OutboundReply(text=(
            "这款我们这里没有。目前在售：" + "、".join(c["name"] for c in cat[:3]) +
            "。报货号或名字我给您报价。"))

    # 目录
    if any(k in text for k in ["有什么", "目录", "有哪些", "价目"]):
        lines = [f'{c["sku"]} {c["name"]} 零售¥{c["retail"]:g}/拿货¥{c["trade"]:g}' for c in get_catalog(d)[:6]]
        return OutboundReply(text="主打这几款：\n" + "\n".join(lines) + "\n报货号或名字，我给您报价。")

    if "价" in text or "多少" in text:
        return OutboundReply(text="哪一款？说个货号（比如 A3）或者名字，发图也行。")

    # 闲聊兜底: 友好+拉回业务
    # 闲聊：友好回一句，自然拉回业务
    if any(k in text for k in ["心情", "吃了吗", "在吗", "忙不忙", "哈哈", "你好呀", "早上好", "晚上好", "辛苦", "天气"]):
        return OutboundReply(text="哈哈，挺好，谢谢关心。您今天想看点什么货？报个货号或名字就行。")
    last = _recall_item(d, uid)
    if last:
        return OutboundReply(text=f"没太听明白🤔 是说 {last['name']} 吗？要的话报个数量；换别的款也行，报货号或名字。")
    # 举例用店里真有的第一款，别让真实客户店的买家照着问演示品
    cat = get_catalog(d)
    if not cat:
        return OutboundReply(text="没太听明白🤔 店里商品马上上架，稍等老板一下。")
    eg = cat[0]
    eg_name = eg["name"][len(eg["sku"]):] if eg["name"].startswith(eg["sku"]) else eg["name"]
    return OutboundReply(text=f"没太听明白🤔 您看点什么货？报个货号或名字，比如「{eg['sku']}」或「{eg_name.strip()}」。")


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
.chat{background:var(--card);border-radius:10px;padding:12px;margin-bottom:12px;box-shadow:0 1px 3px rgba(31,56,100,.08)}
.log{max-height:260px;overflow-y:auto;margin-bottom:8px}
.log div{padding:8px 11px;border-radius:10px;margin-bottom:6px;font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.bot{background:#EEF3FB;color:#1F2733}
.me{background:var(--b);color:#fff;margin-left:22%}
.ask{display:flex;gap:6px}
.ask input{flex:1;padding:9px 11px;border:1px solid #D5DBE7;border-radius:8px;font-size:15px}
.send{background:var(--b);color:#fff;padding:9px 16px}
.quick{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.quick span{font-size:12.5px;background:#EEF3FB;color:var(--b);border-radius:14px;padding:4px 10px;cursor:pointer;border:1px solid #D9E3F3}
.quick span:active{background:#DCE7F7}
.hint{font-size:12.5px;color:#8A93A6;margin-top:6px;line-height:1.6}
.tpl{display:inline-block;font-size:13px;color:var(--b);text-decoration:none;background:#EEF3FB;border:1px solid #D9E3F3;border-radius:14px;padding:5px 11px}
.tpl:active{background:#DCE7F7}
.chk{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:13.5px;color:#5A6B84}
#importResult{margin-top:8px;font-size:13.5px;color:#1F2733}
.shot{margin:8px 0}
.shot img{max-width:190px;max-height:150px;border-radius:8px;border:1px solid #E3DCC9;cursor:zoom-in;display:block}
.shot span{font-size:11.5px;color:#8A93A6}
#lb{position:fixed;inset:0;background:rgba(15,23,42,.9);display:none;align-items:center;justify-content:center;z-index:99;padding:16px}
#lb img{max-width:100%;max-height:100%;border-radius:10px}
</style></head><body>
<header><h1>🏪 店小力 · 老板端</h1><span class="badge" id="aiBadge">AI 接待中</span></header>
<main>
<section class="chat">
 <h2>💬 跟小力说句话，它去办</h2>
 <div id="log" class="log"><div class="bot">您好老板！想让我做什么直接说，比如「今天卖得怎么样」「全部核准」「A3还有多少」「A3拿货价改成30」。回「帮助」看完整能力。</div></div>
 <div class="ask">
  <input id="q" placeholder="例如：今天卖得怎么样" autocomplete="off">
  <button class="send" onclick="ask()">发送</button>
 </div>
 <div class="quick">
  <span onclick="qk('今天卖得怎么样')">今天卖得怎么样</span>
  <span onclick="qk('待办')">待办</span>
  <span onclick="qk('全部核准')">全部核准</span>
  <span onclick="qk('哪些快没货了')">哪些快没货了</span>
  <span onclick="qk('这周什么卖得最好')">本周热销</span>
  <span onclick="qk('帮助')">帮助</span>
 </div>
</section>
<div class="tiles">
 <div class="tile"><b id="tOrders">-</b><span>今日成交</span></div>
 <div class="tile"><b id="tGmv">-</b><span>金额¥</span></div>
 <div class="tile"><b id="tProfit">-</b><span>毛利¥</span></div>
</div>
<section><h2>🟠 待核准 <span id="pCount"></span></h2><div id="pending"></div></section>
<section><h2>📦 库存（≤80 标红）</h2><table id="stock"></table></section>
<section><h2>📄 产品表</h2>
 <div class="row"><a class="tpl" id="tpl0" href="#">下载模板（档口版）</a><a class="tpl" id="tpl1" href="#">下载模板（工厂版）</a></div>
 <div class="row"><input type="file" id="importFile" accept=".xlsx"></div>
 <label class="chk"><input type="checkbox" id="importReplace"> 先清空上次上传的产品</label>
 <div class="row"><button class="ok" id="importBtn" onclick="upload()">上传导入</button></div>
 <pre id="importResult">填好模板选文件，点「上传导入」就行。</pre>
 <div class="hint">说明：表头中英文都认，列顺序随意；带 FOB@数量 列会自动进工厂阶梯价。清空只影响上传过的产品，内置演示商品不受影响。</div>
</section>
<section class="switch"><h2 style="margin:0">🤖 AI 话术开关</h2>
 <button class="toggle" id="aiBtn" onclick="toggleAI()">载入中</button>
 <button class="toggle" id="demoBtn" onclick="toggleDemo()">载入中</button></section>
<section><h2>🌙 今日日报</h2><pre id="report">载入中…</pre></section>
</main>
<div id="lb" onclick="this.style.display='none'"><img id="lbi"></div>
<script>
const KEY=new URLSearchParams(location.search).get('key')||'';
function zoom(src){document.getElementById('lbi').src=src;document.getElementById('lb').style.display='flex'}
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
   ${p.media?`<div class="shot"><img src="/boss/media?key=${KEY}&id=${p.media}" onclick="zoom(this.src)"><span>点击看大图</span></div>`:''}
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
 const dm=s.demo_catalog;document.getElementById('demoBtn').textContent=dm?'演示商品：显示中':'演示商品：已隐藏';
 document.getElementById('demoBtn').className='toggle'+(dm?'':' off');
 document.getElementById('report').textContent=s.report;
}
function push(cls,txt){const l=document.getElementById('log');const d=document.createElement('div');d.className=cls;d.textContent=txt;l.appendChild(d);l.scrollTop=l.scrollHeight}
async function ask(){const i=document.getElementById('q');const t=i.value.trim();if(!t)return;i.value='';push('me',t);
 const r=await api('/boss/ask',{text:t});push('bot',r.reply||'（无响应）');refresh()}
function qk(t){document.getElementById('q').value=t;ask()}
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&document.activeElement.id==='q')ask()});
async function act(id,op){await api('/boss/act',{id,op});refresh()}
async function setStock(sku){const v=parseInt(document.getElementById('n_'+sku).value||'0');await api('/boss/stock',{sku,stock:v});refresh()}
async function toggleAI(){await api('/boss/toggle',{});refresh()}
async function toggleDemo(){await api('/boss/demo_toggle',{});refresh()}
function tplLinks(){const q='/boss/import/template?key='+encodeURIComponent(KEY);
 document.getElementById('tpl0').href=q+'&factory=0';
 document.getElementById('tpl1').href=q+'&factory=1'}
async function upload(){
 const f=document.getElementById('importFile'),b=document.getElementById('importBtn'),o=document.getElementById('importResult');
 if(!f.files||!f.files[0]){o.textContent='还没选文件：先选一个 .xlsx 产品表。';return}
 const fd=new FormData();fd.append('file',f.files[0]);
 const rep=document.getElementById('importReplace').checked?'&replace=1':'';
 b.disabled=true;b.textContent='上传中…';o.textContent='正在导入，稍等一下…';
 try{
  const r=await fetch('/boss/import?key='+encodeURIComponent(KEY)+rep,{method:'POST',body:fd});
  let j={};try{j=await r.json()}catch(e){}
  if(!r.ok){o.textContent='导入失败：'+(j.err||('服务器返回 '+r.status+'，检查 key 或表格格式。'))}
  else{o.textContent=j.text||'导入完成。';f.value='';refresh()}
 }catch(e){o.textContent='导入失败：网络没通，检查网络后重试。'}
 b.disabled=false;b.textContent='上传导入';
}
tplLinks();refresh();setInterval(refresh,8000);
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
    stock_view = [{"sku": c["sku"], "name": c["name"], "stock": d["stock"].get(c["sku"], 0)}
                  for c in get_catalog(d)]
    return {"ai_on": d.get("ai_on", True), "demo_catalog": demo_on(d), "pending": d["pending"],
            "today_orders": len(orders),
            "today_gmv": round(sum(o["amount"] for o in orders)),
            "today_profit": round(sum(o["amount"] - o.get("cost", 0) for o in orders)),
            "stock": stock_view, "report": boss_report(d)}


@router.post("/boss/act")
async def boss_act(request: Request):
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    body = await request.json()
    d = load()
    if _do_act(d, body.get("id"), body.get("op")):
        save(d)
    return {"ok": True}


@router.get("/boss/media")
def boss_media(request: Request):
    """老板端查看顾客发来的付款截图"""
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    from fastapi.responses import FileResponse
    name = request.query_params.get("id", "")
    if not name or "/" in name or ".." in name:
        return JSONResponse({"err": "bad id"}, status_code=400)
    p = MEDIA_DIR / name
    if not p.exists():
        return JSONResponse({"err": "not found"}, status_code=404)
    return FileResponse(str(p), media_type="image/jpeg")


@router.post("/boss/ask")
async def boss_ask(request: Request):
    """老板端对话入口：与微信侧共用同一套 _owner_brain 指令理解。"""
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return {"reply": "您说，想让我做什么？"}
    d = load()
    try:
        r = _owner_brain(text, d)
    except Exception as e:
        record_error(e)
        return {"reply": f"这条我没处理好（{type(e).__name__}）。换个说法试试，或回「帮助」看我会什么。"}
    if r and r.text:
        return {"reply": r.text}
    return {"reply": ("这条我还不会处理 🤔 回「帮助」看我能做什么；"
                      "常用的有：今天卖得怎么样 / 待办 / 全部核准 / A3还有多少 / 上新A3 100个")}


@router.post("/boss/import")
async def boss_import(request: Request):
    """老板端上传产品表（xlsx）→ 商品库 + 库存 + 工厂 FOB 阶梯。?replace=1 先清旧目录。"""
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    import catalog_import as _ci
    form = await request.form()
    up = form.get("file")
    if up is None:
        return JSONResponse({"err": "缺少文件字段 file"}, status_code=400)
    data = await up.read()
    try:
        items, errors = _ci.parse_xlsx(data)
    except Exception as e:
        record_error(e)
        return JSONResponse({"err": f"解析失败：{type(e).__name__}: {e}"[:200]}, status_code=400)
    d = load()
    applied = _ci.apply_to_store(items, d, replace=request.query_params.get("replace") == "1")
    save(d)
    return {"ok": True, **applied, "errors": errors,
            "text": _ci.summary_text(items, errors, applied,
                                     factory=bool((d.get("factory") or {}).get("enabled")),
                                     demo_on=d.get("demo_catalog", True))}


@router.get("/boss/import/template")
def boss_import_template(request: Request):
    """下载给客户填的产品表模板（?factory=0 不带工厂列）。"""
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    import catalog_import as _ci, tempfile
    from fastapi.responses import FileResponse
    p = Path(tempfile.gettempdir()) / "dianxiaoli_catalog_template.xlsx"
    _ci.write_template(str(p), factory=request.query_params.get("factory") != "0")
    return FileResponse(str(p), filename="店小力产品表模板.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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


@router.post("/boss/demo_toggle")
async def boss_demo_toggle(request: Request):
    """内置演示商品显示/隐藏。隐藏后买家只看得到老板自己上传/上新的商品。"""
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    d = load(); d["demo_catalog"] = not demo_on(d); save(d)
    return {"ok": True, "demo_catalog": d["demo_catalog"]}


@router.get("/risk/export")
def risk_export(request: Request):
    """《批发零售小微经营健康画像》导出 — 纯确定性统计, 无 LLM 参与。
    数据源: 本店真实台账（订单/客户/库存/凭证）。度小满风控接口演示端点。"""
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    import statistics
    d = load()
    orders = d["orders"]
    amounts = [o["amount"] for o in orders] or [0]
    by_month, by_buyer = {}, {}
    for o in orders:
        by_month.setdefault(o["ts"][:2], []).append(o["amount"])
        by_buyer.setdefault(o.get("userid", "?"), []).append(o["amount"])
    m_gmv = [sum(v) for v in by_month.values()] or [0]
    repeat = sum(1 for v in by_buyer.values() if len(v) >= 2)
    top5 = sum(sorted((sum(v) for v in by_buyer.values()), reverse=True)[:5])
    total = sum(amounts) or 1
    cat = get_catalog(d)
    stock_val = sum(d["stock"].get(c["sku"], 0) * c.get("trade", 0) for c in cat)
    cogs = sum(o.get("cost", 0) for o in orders)
    verified = [p for p in d["pending"] if p["kind"] == "付款核验"]
    disputed = [p for p in d["pending"] if p["kind"] in ("转人工", "金额异常")]
    inquiries = max(len(d.get("customers", {})), 1)
    stockout = sum(1 for c in cat if d["stock"].get(c["sku"], 0) == 0)

    profile = {
        "export_version": "1.0",
        "merchant_id": "mch_dianxiaoli_demo",
        "segment": "wholesale_retail",
        "window": f"{today_str()} (演示窗口)",
        "consent": {"consent_id": "csnt_demo_001", "consent_ts": now_str(),
                    "note": "商户授权后导出；本演示为样板店数据"},
        "cashflow": {
            "gmv_total": round(sum(amounts), 2),
            "gmv_monthly_avg": round(sum(m_gmv) / max(len(m_gmv), 1), 2),
            "gmv_volatility": round(statistics.pstdev(m_gmv) / (statistics.mean(m_gmv) or 1), 3),
            "order_count": len(orders),
            "avg_order_value": {"mean": round(statistics.mean(amounts), 2),
                                "median": round(statistics.median(amounts), 2)},
            "gross_margin_rate": round((sum(amounts) - cogs) / total, 3),
        },
        "operations": {
            "sku_count": len(cat),
            "stock_value_at_cost": round(stock_val, 2),
            "inventory_turnover_days": round(stock_val / (cogs or 1), 1),
            "stockout_sku_rate": round(stockout / max(len(cat), 1), 3),
            "restock_waitlist": len(d["waitlist"]),
        },
        "customers": {
            "active_buyers": len(by_buyer),
            "total_contacts": len(d.get("customers", {})),
            "repeat_purchase_rate": round(repeat / max(len(by_buyer), 1), 2),
            "top5_concentration": round(top5 / total, 2),
            "regular_ratio": round(len(d["regulars"]) / inquiries, 2),
        },
        "fulfillment": {
            "payment_voucher_count": len(verified),
            "dispute_escalation_count": len(disputed),
            "pending_approval": len(d["pending"]),
            "response_sla_seconds": 3,
        },
        "quality_assurance": {
            "scenario_gate": "28 条中文行为断言 + 41 条图片识别测试, 通过率 100%",
            "gate_threshold": 0.95,
            "daily_audit": _audit_summary_for_export(),
        },
        "metadata": {
            "generated_at": now_str(),
            "method": "deterministic_statistics_no_llm",
            "source": "merchant_ledger (live)",
            "spec": "批发零售小微经营健康画像数据输出规范 v1",
        },
    }
    return profile


def _audit_summary_for_export():
    try:
        import agent_c_audit as _ac
        st = _ac.status()
        ld = st.get("last_daily") or {}
        return {"enabled": True, "method": "deterministic_rules_no_llm",
                "last_run": ld.get("date"), "last_defects": ld.get("defects")}
    except Exception:
        return {"enabled": False}


@router.get("/audit/daily")
def audit_daily(request: Request):
    """Agent C 每日审计报告（默认今天；?date=YYYY-MM-DD 看历史；?run=1 现算）。"""
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    import agent_c_audit as _ac
    day = request.query_params.get("date") or _cl.today()
    if request.query_params.get("run") == "1":
        r = _ac.run_daily(day, push=request.query_params.get("push") == "1")
    else:
        r = _ac.load_report("audit", day) or _ac.audit_day(day, __import__(__name__))
    r["text"] = _ac.daily_text(r)
    return r


@router.get("/audit/weekly")
def audit_weekly(request: Request):
    """Agent C 质检周报（最近 7 天；?end=YYYY-MM-DD 指定截止日）。"""
    if not _auth(request):
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    import agent_c_audit as _ac
    end = request.query_params.get("end") or _cl.today()
    r = _ac.run_weekly(end, push=request.query_params.get("push") == "1")
    r["text"] = _ac.weekly_text(r)
    return r


def boss_notify_status():
    """老板推送：配没配群机器人、推成功/失败几条、最近三条。"""
    try:
        import boss_notify as _bn
        return {"configured": _bn.configured(),
                "sent": sum(1 for x in _bn.PUSH_LOG if x.get("ok")),
                "failed": sum(1 for x in _bn.PUSH_LOG if not x.get("ok")),
                "last": _bn.PUSH_LOG[-3:]}
    except Exception as e:
        return {"configured": False, "sent": 0, "failed": 0, "last": [], "why": str(e)[:80]}


@router.get("/status")
def status():
    envs = {k: ("✓ 已配置" if os.environ.get(k) else "✗ 缺失")
            for k in ["WECOM_CORP_ID", "WECOM_KF_SECRET", "WECOM_TOKEN", "WECOM_AES_KEY", "BOSS_KEY"]}
    d = load()
    try:
        import dianxiaoli_brain as _llm
        brain_status = _llm.status()
    except Exception as e:
        brain_status = {"error": str(e)}
    try:
        import wecom_service as _ws
        adapter_stats = getattr(_ws._adapter, "stats", {}) if getattr(_ws, "_adapter", None) else {}
    except Exception:
        adapter_stats = {}
    try:
        import feishu_sync as _fs
        feishu = {"configured": bool(_fs.APP_ID and _fs.APP_SECRET and _fs.BITABLE),
                  "last_sync": _fs._last_sync}
    except Exception as e:
        feishu = {"error": str(e)[:80]}
    try:
        import agent_c_audit as _ac
        audit = _ac.status()
    except Exception as e:
        audit = {"error": str(e)[:80]}
    return {"service": "店小力 AI 店员", "env": envs, "brain": brain_status, "feishu": feishu,
            "audit": audit,
            "adapter": adapter_stats, "data_dir": str(DATA_DIR), "ai_on": d.get("ai_on", True),
            "demo_catalog": demo_on(d),
            "pending": len(d["pending"]), "orders_total": len(d["orders"]),
            "last_error": LAST_ERROR if LAST_ERROR["text"] else "无",
            "notify": {"sent": sum(1 for x in NOTIFY_LOG if x.get("ok")),
                       "failed": sum(1 for x in NOTIFY_LOG if not x.get("ok")),
                       "last": NOTIFY_LOG[-3:]},
            "boss_notify": boss_notify_status(),
            "提示": "出错时这里会给出人话修复建议; /boss?key=BOSS_KEY 老板端; /risk/export?key=BOSS_KEY 风控画像导出; /egress 查出口IP"}
