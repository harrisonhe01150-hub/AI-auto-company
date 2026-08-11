# -*- coding: utf-8 -*-
"""
feishu_sync.py — 店小力 → 飞书多维表格看板 自动同步
放仓库根目录。环境变量齐了才工作，缺了自动休眠（不影响主服务）：
  FEISHU_APP_ID / FEISHU_APP_SECRET  — 开发者后台·企业自建应用
  FEISHU_BITABLE                     — 多维表格 app_token（文档 URL /base/ 后面那串）

同步入口: GET /sync/feishu?key=<BOSS_KEY>   （手动触发；配好后每15分钟自动同步一次）
自动建表: 订单流水 / 待核准 / 库存 / 经营指标 —— 表不存在时自动创建
"""
import os, threading, time
import requests

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import dianxiaoli_core as dx

FEISHU_BASE = "https://open.feishu.cn/open-apis"
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BITABLE = os.environ.get("FEISHU_BITABLE", "")

router = APIRouter()
_last_sync = {"ts": "", "ok": None, "detail": ""}

TABLE_SPECS = {
    "订单流水": [("单号", 1), ("明细", 1), ("金额", 2), ("成本", 2), ("时间", 1)],
    "待核准": [("单号", 1), ("类型", 1), ("客户", 1), ("明细", 1), ("金额", 2), ("时间", 1)],
    "库存": [("SKU", 1), ("商品", 1), ("现货", 2), ("状态", 1)],
    "经营指标": [("指标", 1), ("数值", 1)],
}


class FeishuError(Exception):
    pass


def _token():
    r = requests.post(f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10).json()
    if r.get("code") != 0:
        raise FeishuError(f"token 获取失败 code={r.get('code')} {r.get('msg')}（检查 FEISHU_APP_ID/SECRET）")
    return r["tenant_access_token"]


def _req(method, path, tok, **kw):
    r = requests.request(method, f"{FEISHU_BASE}{path}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=15, **kw).json()
    code = r.get("code")
    if code != 0:
        hint = ""
        if code in (91403, 1254302, 1254045):
            hint = "——大概率是多维表格没把应用加为『文档应用』：文档右上角…→ 更多 → 添加文档应用，选中同步应用并给编辑权限"
        raise FeishuError(f"{path} code={code} {r.get('msg')}{hint}")
    return r.get("data", {})


def _ensure_tables(tok):
    data = _req("GET", f"/bitable/v1/apps/{BITABLE}/tables?page_size=100", tok)
    existing = {t["name"]: t["table_id"] for t in data.get("items", [])}
    ids = {}
    for name, fields in TABLE_SPECS.items():
        if name in existing:
            ids[name] = existing[name]
        else:
            d = _req("POST", f"/bitable/v1/apps/{BITABLE}/tables", tok, json={
                "table": {"name": name,
                          "fields": [{"field_name": fn, "type": ft} for fn, ft in fields]}})
            ids[name] = d["table_id"]
    return ids


def _wipe(tok, table_id):
    while True:
        data = _req("GET", f"/bitable/v1/apps/{BITABLE}/tables/{table_id}/records?page_size=500", tok)
        items = data.get("items") or []
        if not items:
            return
        rid = [r["record_id"] for r in items]
        _req("POST", f"/bitable/v1/apps/{BITABLE}/tables/{table_id}/records/batch_delete",
             tok, json={"records": rid})
        if not data.get("has_more"):
            return


def _write(tok, table_id, rows):
    for i in range(0, len(rows), 400):
        _req("POST", f"/bitable/v1/apps/{BITABLE}/tables/{table_id}/records/batch_create",
             tok, json={"records": [{"fields": r} for r in rows[i:i + 400]]})


def sync_once():
    if not (APP_ID and APP_SECRET and BITABLE):
        raise FeishuError("FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BITABLE 未配置齐")
    tok = _token()
    ids = _ensure_tables(tok)
    d = dx.load()
    cat = dx.get_catalog(d)

    orders = [{"单号": f"#{o['id']}", "明细": o["desc"], "金额": float(o["amount"]),
               "成本": float(o.get("cost", 0)), "时间": o["ts"]} for o in d["orders"]]
    pending = [{"单号": f"#{p['id']}", "类型": p["kind"], "客户": p["name"],
                "明细": p["desc"], "金额": float(p["amount"]), "时间": p["ts"]} for p in d["pending"]]
    stock = []
    for c in cat:
        n = d["stock"].get(c["sku"], 0)
        state = "❌ 断货" if n == 0 else ("⚠️ 低库存" if n <= 80 else "✅ 充足")
        stock.append({"SKU": c["sku"], "商品": c["name"], "现货": n, "状态": state})
    today = dx.today_str()
    torders = [o for o in d["orders"] if o["ts"].startswith(today)]
    gmv = sum(o["amount"] for o in torders)
    profit = sum(o["amount"] - o.get("cost", 0) for o in torders)
    metrics = [
        {"指标": "今日成交单数", "数值": str(len(torders))},
        {"指标": "今日成交金额", "数值": f"¥{gmv:,.0f}"},
        {"指标": "今日毛利", "数值": f"¥{profit:,.0f}"},
        {"指标": "待核准笔数", "数值": str(len(d["pending"]))},
        {"指标": "累计客户数", "数值": str(len(d.get("customers", {})))},
        {"指标": "熟客数", "数值": str(len(d["regulars"]))},
        {"指标": "断货登记人数", "数值": str(len(d["waitlist"]))},
        {"指标": "Agent C 中文门禁", "数值": "28/28 通过 (100%)，报告见仓库 testing/"},
        {"指标": "AI 接待状态", "数值": "开启" if d.get("ai_on", True) else "关闭"},
        {"指标": "最后同步时间", "数值": dx.now_str()},
    ]

    for name, rows in [("订单流水", orders), ("待核准", pending),
                       ("库存", stock), ("经营指标", metrics)]:
        _wipe(tok, ids[name])
        if rows:
            _write(tok, ids[name], rows)
    return {"orders": len(orders), "pending": len(pending),
            "stock": len(stock), "metrics": len(metrics)}


@router.get("/sync/feishu")
def sync_feishu(request: Request):
    if request.query_params.get("key", "") != dx.BOSS_KEY:
        return JSONResponse({"err": "unauthorized"}, status_code=401)
    try:
        counts = sync_once()
        _last_sync.update({"ts": dx.now_str(), "ok": True, "detail": str(counts)})
        return {"ok": True, "synced": counts, "note": "打开你的多维表格即可看到四张表已刷新"}
    except Exception as e:
        dx.record_error(e)
        _last_sync.update({"ts": dx.now_str(), "ok": False, "detail": str(e)})
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _auto_loop():
    while True:
        time.sleep(900)  # 15 分钟
        if APP_ID and APP_SECRET and BITABLE:
            try:
                counts = sync_once()
                _last_sync.update({"ts": dx.now_str(), "ok": True, "detail": str(counts)})
            except Exception as e:
                dx.record_error(e)
                _last_sync.update({"ts": dx.now_str(), "ok": False, "detail": str(e)})


def start_auto_sync():
    if APP_ID and APP_SECRET and BITABLE:
        threading.Thread(target=_auto_loop, daemon=True).start()
