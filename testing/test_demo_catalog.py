# -*- coding: utf-8 -*-
"""演示商品可关闭（路线图序 10b）

真实客户店不能拿内置的 A3 保温壶等 14 款演示品报价。
老板一句「关闭演示商品」→ 店里只剩他自己上新/导表的商品；「开启演示商品」恢复。
"""
import os, sys, tempfile
TMP = tempfile.mkdtemp(prefix="dxl_demo_")
os.environ.update(WECOM_CORP_ID="ww", WECOM_KF_SECRET="s", WECOM_TOKEN="t", WECOM_AES_KEY="A"*43,
                  BOSS_KEY="k", LLM_ENABLED="0", AUDIT_SCHEDULER="0", DATA_DIR=TMP)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import catalog_import as ci
import dianxiaoli_core as dx
from wecom_core import InboundMessage
from fastapi.testclient import TestClient
import wecom_service

P = F = 0
def chk(n, cond, e=""):
    global P, F
    P, F = (P+1, F) if cond else (P, F+1)
    print(("PASS  " if cond else "FAIL  ") + n + ("" if cond else "   <- " + str(e)[:200]))

def m(u, t):
    return InboundMessage(channel="w", msg_id="x", conversation_id="c", sender_id=u, account_id="kfA",
                          msg_type="text", text=t, media_bytes=None, media_id="", raw={})
def say(u, t):
    r = dx.brain(m(u, t)); return r.text if r else ""

CUSTOM = [{"sku": "P1", "name": "P1不锈钢阀门DN50", "retail": 300, "trade": 200, "cost": 150, "moq": 10, "stock": 300},
          {"sku": "P2", "name": "P2法兰盘", "retail": 80, "trade": 55, "cost": 40, "moq": 10, "stock": 200},
          {"sku": "P3", "name": "P3密封圈", "retail": 6, "trade": 3.5, "cost": 2, "moq": 50, "stock": 900},
          {"sku": "P4", "name": "P4压力表", "retail": 120, "trade": 88, "cost": 60, "moq": 10, "stock": 40}]

def fresh(demo=None, custom=None, boss="boss1"):
    """重建一个干净的店：demo=None 表示不写这个键（模拟旧存档）"""
    d = dx._default_data()
    if demo is None:
        d.pop("demo_catalog", None)
    else:
        d["demo_catalog"] = demo
    d["custom_skus"] = [dict(c) for c in (custom or [])]
    for c in (custom or []):
        d["stock"][c["sku"]] = c["stock"]
    d["boss_userid"] = boss
    dx.save(d)
    return d

# ── 1. 默认值与向后兼容 ──
chk("默认数据里 demo_catalog 为 True", dx._default_data().get("demo_catalog") is True)
chk("旧存档缺 demo_catalog 键时按显示处理", dx.demo_on({"custom_skus": []}) is True)
d = fresh(demo=None, custom=CUSTOM)
chk("旧存档 get_catalog 仍是 14 款内置 + custom", len(dx.get_catalog(d)) == len(dx.CATALOG) + 4,
    len(dx.get_catalog(d)))

# ── 2. 关闭 / 开启后的目录 ──
d = fresh(demo=False, custom=CUSTOM)
cat = dx.get_catalog(d)
chk("关闭后只剩 custom_skus", [c["sku"] for c in cat] == ["P1", "P2", "P3", "P4"], [c["sku"] for c in cat])
chk("关闭后没有任何内置演示品", not any(c["sku"] in {x["sku"] for x in dx.CATALOG} for c in cat), cat)
d = fresh(demo=True, custom=CUSTOM)
chk("开启时 14 款内置 + 4 款自有", len(dx.get_catalog(d)) == 18, len(dx.get_catalog(d)))
chk("开启时 find_item 认得 A3", (dx.find_item("A3", d) or {}).get("sku") == "A3")
d = fresh(demo=False, custom=CUSTOM)
chk("关闭后 find_item 找不到 A3", dx.find_item("A3多少钱", d) is None)
chk("关闭后 find_item 仍认得自有货号", (dx.find_item("P2多少钱", d) or {}).get("sku") == "P2")
chk("d=None 时 find_item 仍走内置目录（未受影响）", (dx.find_item("A3") or {}).get("sku") == "A3")

# ── 3. 老板指令四种措辞 ──
fresh(demo=True, custom=CUSTOM)
t = say("boss1", "关闭演示商品")
chk("老板说「关闭演示商品」", "演示商品已隐藏" in t and "4 款" in t, t)
chk("关闭后落盘", dx.load().get("demo_catalog") is False)
fresh(demo=True, custom=CUSTOM)
chk("老板说「关掉演示目录」", "演示商品已隐藏" in say("boss1", "关掉演示目录"))
fresh(demo=True, custom=CUSTOM)
chk("老板说「把示例商品隐藏」", "演示商品已隐藏" in say("boss1", "把示例商品隐藏"))
fresh(demo=True, custom=[])
t = say("boss1", "关闭演示商品")
chk("没自有商品时提示下一步怎么做", "还没有上传自己的商品" in t and "上传产品表" in t, t)
chk("空目录文案不报数量", "0 款" not in t, t)
t = say("boss1", "开启演示商品")
chk("老板说「开启演示商品」", "演示商品已恢复显示" in t and "14 款" in t and "关闭演示商品" in t, t)
chk("开启后落盘", dx.load().get("demo_catalog") is True)
fresh(demo=False, custom=CUSTOM)
chk("老板说「恢复演示商品」", "演示商品已恢复显示" in say("boss1", "恢复演示商品"))
chk("恢复后目录回到 18 款", len(dx.get_catalog(dx.load())) == 18)
chk("帮助里有演示开关这一行", "关闭演示商品 / 开启演示商品" in say("boss1", "帮助"), say("boss1", "帮助"))

# ── 4. 买家侧：没有这款 ──
fresh(demo=False, custom=CUSTOM)
t = say("cust1", "A3多少钱")
chk("关闭后买家问 A3 得到「这款我们这里没有」", "这款我们这里没有" in t, t)
chk("并列出前 3 款在售", "P1不锈钢阀门DN50、P2法兰盘、P3密封圈" in t and "P4压力表" not in t, t)
chk("并告诉买家下一步", "报货号或名字" in t, t)
t = say("cust2", "保温壶什么价")
chk("关闭后买家问保温壶也答没有这款", "这款我们这里没有" in t, t)
t = say("cust6", "在线吗")
chk("单字关键词不算问货：「在线吗」不回「没有这款」", "这款我们这里没有" not in t and t, t)
fresh(demo=False, custom=CUSTOM[:1])
t = say("cust8", "在线吗")
chk("兜底举例用店里真有的货，不再拿演示品 A3 举例", "A3" not in t and "P1" in t, t)
fresh(demo=False, custom=CUSTOM)
t = say("cust7", "杯子多少钱")
chk("双字关键词仍算问货：「杯子多少钱」回「没有这款」", "这款我们这里没有" in t, t)
fresh(demo=False, custom=[])
t = say("cust3", "A3多少钱")
chk("目录为空时的文案", t == "这款我们这里没有，目前店里还没上架商品，老板马上补。", t)
fresh(demo=True, custom=CUSTOM)
t = say("cust4", "A3多少钱")
chk("回归：开启时买家问 A3 仍正常报价", "A3保温壶500ml" in t and "45" in t and "这款我们这里没有" not in t, t)
t = say("cust5", "P1多少钱")
chk("关掉与否，自有商品都能报价", "P1不锈钢阀门DN50" in t and "300" in t, t)

# ── 5. 端点 ──
c = TestClient(wecom_service.app)
fresh(demo=True, custom=CUSTOM)
chk("/boss/demo_toggle 无 key 401",
    c.post("/boss/demo_toggle").status_code == 401 and c.post("/boss/demo_toggle").json() == {"err": "unauthorized"})
r = c.post("/boss/demo_toggle?key=k")
chk("/boss/demo_toggle 翻转为隐藏", r.status_code == 200 and r.json() == {"ok": True, "demo_catalog": False}, r.text)
r = c.post("/boss/demo_toggle?key=k")
chk("/boss/demo_toggle 再点一次恢复", r.json() == {"ok": True, "demo_catalog": True}, r.text)
s = c.get("/boss/state?key=k").json()
chk("/boss/state 带 demo_catalog", s.get("demo_catalog") is True, s.get("demo_catalog"))
chk("/boss/state 库存表包含老板自己的商品（修 stock_view 只读内置目录的 bug）",
    "P1" in [x["sku"] for x in s["stock"]] and len(s["stock"]) == 18, [x["sku"] for x in s["stock"]])
c.post("/boss/demo_toggle?key=k")
s = c.get("/boss/state?key=k").json()
chk("隐藏后库存表只剩自有 4 款",
    [x["sku"] for x in s["stock"]] == ["P1", "P2", "P3", "P4"] and s["demo_catalog"] is False, s["stock"])

# ── 6. 老板网页 ──
h = c.get("/boss?key=k").text
chk("老板网页有演示商品按钮", 'id="demoBtn"' in h and "/boss/demo_toggle" in h)
chk("按钮两种文字都在", "演示商品：显示中" in h and "演示商品：已隐藏" in h)

# ── 7. 导表摘要提示 ──
applied = {"added": 2, "updated": 0, "total": 2}
t_on = ci.summary_text([], [], applied, demo_on=True)
t_off = ci.summary_text([], [], applied, demo_on=False)
chk("demo 开着时导表摘要提醒关演示商品", "关闭演示商品" in t_on, t_on)
chk("demo 关着时不再啰嗦", "关闭演示商品" not in t_off and "导入完成" in t_off, t_off)
chk("summary_text 默认仍带提示（默认演示商品是开的）", "关闭演示商品" in ci.summary_text([], [], applied))

print(f"\n结果: {P} passed, {F} failed")
sys.exit(1 if F else 0)
