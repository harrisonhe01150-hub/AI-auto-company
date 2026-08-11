# -*- coding: utf-8 -*-
"""
duxiaoman_risk_exporter 演示版 — 从模拟台账生成《经营健康画像》JSON
按规范v1实现: 纯确定性统计, 无LLM参与。演示数据为合成的批发档口3个月流水。
"""
import json, random, statistics
from datetime import datetime, timedelta

random.seed(42)

# ── 1. 合成一份"客户A风格"的3个月台账 (演示用; 生产环境读真实订单库) ──
buyers = [f"buyer_{i:03d}" for i in range(60)]
regulars = buyers[:22]                       # 熟客
orders, t0 = [], datetime(2026, 5, 1)
for d in range(92):
    day = t0 + timedelta(days=d)
    for _ in range(random.randint(2, 8)):    # 每日2-8单
        b = random.choice(regulars) if random.random() < 0.62 else random.choice(buyers)
        orders.append({
            "date": day.strftime("%Y-%m-%d"),
            "buyer": b,
            "amount": round(random.lognormvariate(6.8, 0.7), 2),   # 客单价长尾分布
            "verified": random.random() < 0.97,                     # OCR核验
            "disputed": random.random() < 0.013,
            "resp_seconds": round(random.expovariate(1/2.5), 1),
        })

inventory = {"skus": 300, "avg_stock_value": 186000, "period_cogs": 720000}
stockout_events, inquiry_count = 31, 520

# ── 2. 按规范聚合指标 ──
monthly = {}
for o in orders:
    monthly.setdefault(o["date"][:7], []).append(o["amount"])
m_gmv = {m: sum(v) for m, v in monthly.items()}
gmv_list = list(m_gmv.values())
amounts = [o["amount"] for o in orders]
buyer_orders = {}
for o in orders:
    buyer_orders.setdefault(o["buyer"], []).append(o["amount"])
repeat = sum(1 for v in buyer_orders.values() if len(v) >= 2)
top5 = sum(sorted((sum(v) for v in buyer_orders.values()), reverse=True)[:5])

profile = {
  "export_version": "1.0",
  "merchant_id": "mch_demo_a3f9",
  "segment": "wholesale",
  "window": "2026-05-01/2026-07-31",
  "consent": {"consent_id": "csnt_demo_001", "consent_ts": "2026-07-30T09:12:00+08:00"},
  "cashflow": {
    "gmv_monthly_avg": round(sum(gmv_list)/len(gmv_list), 2),
    "gmv_volatility": round(statistics.pstdev(gmv_list)/statistics.mean(gmv_list), 3),
    "order_count_monthly": round(len(orders)/3),
    "avg_order_value": {"mean": round(statistics.mean(amounts), 2),
                        "median": round(statistics.median(amounts), 2)},
  },
  "operations": {
    "inventory_turnover_days": round(inventory["avg_stock_value"]/(inventory["period_cogs"]/92), 1),
    "stockout_rate": round(stockout_events/inquiry_count, 3),
  },
  "customers": {
    "repeat_purchase_rate": round(repeat/len(buyer_orders), 2),
    "active_buyers": len(buyer_orders),
    "top5_concentration": round(top5/sum(amounts), 2),
  },
  "fulfillment": {
    "payment_verified_rate": round(sum(o["verified"] for o in orders)/len(orders), 3),
    "dispute_rate": round(sum(o["disputed"] for o in orders)/len(orders), 4),
    "response_sla_seconds": round(statistics.median(o["resp_seconds"] for o in orders), 1),
  },
  "metadata": {"sample_size": {"orders": len(orders)}, "coverage": 0.93,
               "generated_at": "2026-08-06T02:00:00+08:00"},
}
print(json.dumps(profile, ensure_ascii=False, indent=2))
