# -*- coding: utf-8 -*-
"""
catalog_import.py — 产品表 → 店小力商品库（路线图 2.6 演示店 / 新客户交付第一步）

一张 xlsx 进来，商品目录、库存、双价、起批量、工厂 FOB 阶梯全部建好。表头中英文都认，列顺序随意。

认得的列（大小写、空格无所谓）：
  货号/型号/SKU/Item No      名称/品名/Name/Description
  零售价/Retail              拿货价/批发价/Trade/Wholesale
  进价/成本/Cost             起批/起批量/MOQ
  库存/数量/Stock/Qty
  工厂字段：FOB起订/MOQ FOB    FOB@500 / FOB@2000 / FOB@5000 …（列名里带 FOB@数量，值是美元单价）
             交期/Lead Days  港口/Port

用法：
  python catalog_import.py 产品表.xlsx            # 导入到本地 dianxiaoli_data.json（演示店）
  python catalog_import.py --template 模板.xlsx    # 生成给客户填的空模板
  线上：POST /boss/import?key=BOSS_KEY  (multipart 字段 file)
"""
import io, re, sys
from typing import Dict, List, Tuple

HEADER_MAP = {
    "sku":    ("货号", "型号", "sku", "item no", "item", "编码", "款号", "code"),
    "name":   ("名称", "品名", "name", "description", "产品", "商品", "desc"),
    "retail": ("零售价", "零售", "retail", "rrp", "list price"),
    "trade":  ("拿货价", "批发价", "批发", "trade", "wholesale", "拿货"),
    "cost":   ("进价", "成本", "cost", "成本价"),
    "moq":    ("起批量", "起批", "moq"),
    "stock":  ("库存", "数量", "stock", "qty", "quantity", "现货"),
    "moq_fob": ("fob起订", "fob 起订", "moq fob", "fob moq", "起订量fob", "fob起订量"),
    "lead_days": ("交期", "lead days", "lead time", "货期"),
    "port":   ("港口", "port"),
}
FOB_TIER_RE = re.compile(r"fob\s*@?\s*(\d[\d,]*)", re.I)


def _norm(h) -> str:
    return re.sub(r"\s+", " ", str(h or "")).strip().lower()


def _col_key(header: str):
    h = _norm(header)
    if not h:
        return None
    m = FOB_TIER_RE.search(h)
    if m and "起订" not in h and "moq" not in h:
        return ("fob_tier", int(m.group(1).replace(",", "")))
    for key, names in HEADER_MAP.items():
        if any(h == n or h.startswith(n) for n in names):
            return key
    return None


def _num(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(str(v).replace(",", "").replace("¥", "").replace("$", "").strip())
    except Exception:
        return default


def parse_xlsx(data: bytes) -> Tuple[List[dict], List[str]]:
    """返回 (items, errors)。items 每项：sku,name,retail,trade,cost,moq,stock,(fob_tiers,moq_fob,lead_days,port)"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], ["空表"]
    # 找表头行：第一行里能认出 sku 和 name 的
    hdr_idx, colmap = None, {}
    for i, row in enumerate(rows[:10]):
        cm = {}
        for j, cell in enumerate(row):
            k = _col_key(cell)
            if k:
                cm[j] = k
        if "sku" in cm.values() and ("name" in cm.values() or "retail" in cm.values() or "trade" in cm.values()):
            hdr_idx, colmap = i, cm
            break
    if hdr_idx is None:
        return [], ["找不到表头：至少要有「货号」和「名称/零售价/拿货价」列"]

    items, errors, seen = [], [], set()
    for r_i, row in enumerate(rows[hdr_idx + 1:], start=hdr_idx + 2):
        rec, tiers = {}, []
        for j, key in colmap.items():
            v = row[j] if j < len(row) else None
            if isinstance(key, tuple):
                usd = _num(v)
                if usd:
                    tiers.append({"min": key[1], "usd": round(usd, 2)})
            else:
                rec[key] = v
        sku = str(rec.get("sku") or "").strip().upper()
        if not sku:
            continue
        if sku in seen:
            errors.append(f"第 {r_i} 行：货号 {sku} 重复，后者覆盖前者")
            items = [x for x in items if x["sku"] != sku]
        seen.add(sku)
        retail, trade = _num(rec.get("retail")), _num(rec.get("trade"))
        if retail is None and trade is None and not tiers:
            errors.append(f"第 {r_i} 行：{sku} 没有任何价格，跳过")
            continue
        if trade is None and retail is not None:
            trade = round(retail * 0.7, 1)
        if retail is None and trade is not None:
            retail = round(trade * 1.4, 1)
        item = {
            "sku": sku,
            "name": str(rec.get("name") or sku).strip(),
            "retail": retail, "trade": trade,
            "cost": _num(rec.get("cost"), 0) or 0,
            "moq": int(_num(rec.get("moq"), 0) or 0) or None,
            "stock": int(_num(rec.get("stock"), 0) or 0),
        }
        if item["moq"] is None:
            item.pop("moq")
        if tiers:
            item["fob_tiers"] = sorted(tiers, key=lambda t: t["min"])
        if _num(rec.get("moq_fob")):
            item["moq_fob"] = int(_num(rec.get("moq_fob")))
        if _num(rec.get("lead_days")):
            item["lead_days"] = int(_num(rec.get("lead_days")))
        if rec.get("port"):
            item["port"] = str(rec["port"]).strip()
        items.append(item)
    return items, errors


def apply_to_store(items: List[dict], d: dict, replace: bool = False) -> Dict[str, int]:
    """写进 d["custom_skus"] 和 d["stock"]。replace=True 时先清掉旧的自定义目录。"""
    if replace:
        d["custom_skus"] = []
    existing = {c["sku"]: c for c in d.get("custom_skus", [])}
    added = updated = 0
    for it in items:
        stock = it.pop("stock", 0)
        if it["sku"] in existing:
            existing[it["sku"]].update(it); updated += 1
        else:
            existing[it["sku"]] = it; added += 1
        d.setdefault("stock", {})[it["sku"]] = stock
    d["custom_skus"] = list(existing.values())
    return {"added": added, "updated": updated, "total": len(d["custom_skus"])}


def summary_text(items, errors, applied, factory=False, demo_on: bool = True) -> str:
    lines = [f"✅ 产品表导入完成：新增 {applied['added']} 款，更新 {applied['updated']} 款，目录共 {applied['total']} 款。"]
    with_fob = sum(1 for i in items if i.get("fob_tiers"))
    if with_fob:
        lines.append(f"其中 {with_fob} 款带 FOB 阶梯价" + ("" if factory else "（开启工厂模式后生效：回「开启工厂模式」）"))
    if errors:
        lines.append(f"⚠️ {len(errors)} 条提示：")
        lines += [f" · {e}" for e in errors[:8]]
    lines.append("试一句：报个货号问价，或英文问 FOB。")
    if demo_on:
        lines.append("提示：内置演示商品还在显示中，正式接待前对我说「关闭演示商品」。")
    return "\n".join(lines)


def write_template(path: str, factory: bool = True):
    """给客户填的空模板：两行示例 + 说明。"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "产品表"
    heads = ["货号", "名称", "零售价", "拿货价", "进价", "起批量", "库存"]
    if factory:
        heads += ["FOB起订", "FOB@500", "FOB@2000", "FOB@5000", "交期(天)", "港口"]
    ws.append(heads)
    for c in ws[1]:
        c.font = Font(bold=True); c.fill = PatternFill("solid", fgColor="DCE6F1")
    if factory:
        ws.append(["A3", "A3 保温壶 500ml", 45, 32, 20, 50, 1400, 500, 5.4, 5.2, 4.9, 25, "Ningbo"])
        ws.append(["P1", "P1 工业阀门 DN50", 300, 200, 150, 10, 300, 100, 40, 36, 33, 30, "Shanghai"])
    else:
        ws.append(["A3", "A3 保温壶 500ml", 45, 32, 20, 50, 1400])
        ws.append(["B1", "B1 玻璃杯", 12, 8, 5, 50, 800])
    ws2 = wb.create_sheet("填写说明")
    tips = ["货号：唯一编码，字母+数字，买家问价时会直接用它",
            "零售价 / 拿货价：人民币；拿货价留空会按零售价七折算",
            "进价：只用于算毛利，买家永远看不到",
            "起批量：多少件起走拿货价；留空按店铺默认",
            "库存：当前现货数；买家永远看不到具体数字，只会被告知有/不多/没货",
            "FOB@数量：该数量档的美元单价，列名里的数字就是数量档；可增减列，如 FOB@1000",
            "FOB起订：FOB 条件下的最低起订量；交期按天；港口默认 Ningbo",
            "示例两行填完请删掉"]
    for t in tips:
        ws2.append([t])
    for col, w in zip("ABCDEFGHIJKLM", [10, 26, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 12]):
        ws.column_dimensions[col].width = w
    wb.save(path)


if __name__ == "__main__":
    import argparse, os
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", nargs="?")
    ap.add_argument("--template", metavar="OUT.xlsx", help="生成空模板")
    ap.add_argument("--no-factory", action="store_true", help="模板不带工厂列")
    ap.add_argument("--replace", action="store_true", help="清掉旧的自定义目录再导入")
    a = ap.parse_args()
    if a.template:
        write_template(a.template, factory=not a.no_factory); print("模板已生成：", a.template); sys.exit(0)
    if not a.xlsx:
        ap.print_help(); sys.exit(1)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dianxiaoli_core as dx
    items, errors = parse_xlsx(open(a.xlsx, "rb").read())
    d = dx.load(); applied = apply_to_store(items, d, replace=a.replace); dx.save(d)
    print(summary_text(items, errors, applied, factory=bool((d.get("factory") or {}).get("enabled"))))
