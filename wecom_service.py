# -*- coding: utf-8 -*-
"""
wecom_service.py — 店小力·微信客服回调服务（Railway 测试版）
放在仓库根目录，与 wecom_core/ 同级。
启动命令: uvicorn wecom_service:app --host 0.0.0.0 --port $PORT

阶段1（当前）: 回调验证 + 收发链路打通——顾客发什么，小力都礼貌应答（echo 桩）。
阶段2: 把 stage1_brain 换成接 sales_brain 的真实入口（见文件底部注释）。
"""
import os
from fastapi import FastAPI
from wecom_core import build_router, OutboundReply

app = FastAPI(title="dianxiaoli-wecom")

@app.get("/")
def health():
    return {"service": "店小力 wecom callback", "status": "ok"}

@app.get("/egress")
def egress():
    """返回 Railway 当前出口 IP——把它填进企微后台的可信 IP"""
    import requests
    try:
        ip = requests.get("https://api.ipify.org", timeout=6).text.strip()
        return {"railway_egress_ip": ip, "note": "把这个IP加入: ①店小力AI店员应用的企业可信IP ②如有提示,微信客服API处同样填它"}
    except Exception as e:
        return {"error": str(e)}

def stage1_brain(msg):
    """打通链路用的演示桩: 欢迎语 + 回声 + 图片确认"""
    if msg.text == "__EVENT_ENTER_SESSION__":
        return OutboundReply(text="您好，我是店小力的AI店员小力，通道已打通，想看点什么随时说～")
    if msg.msg_type == "image":
        return OutboundReply(text=f"收到您的图片（{len(msg.media_bytes or b'')}字节），链路测试正常！")
    return OutboundReply(text=f"小力收到：{msg.text}（回调链路测试成功）")

app.include_router(build_router(on_message=stage1_brain))

# ── 阶段2 接真实销售大脑（打通后替换 stage1_brain）──────────────
# from sales_brain import handle  # 按你仓库实际入口调整
# def real_brain(msg):
#     reply = handle(conversation_id=msg.conversation_id, sender=msg.sender_id,
#                    text=msg.text, media=msg.media_bytes, channel="wecom")
#     return OutboundReply(text=reply) if reply else None
