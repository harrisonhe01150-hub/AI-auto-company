# -*- coding: utf-8 -*-
"""
wecom_service.py — 店小力·微信客服服务 v2（真大脑 + 老板端 + 自检）
放在仓库根目录，与 wecom_core/ 同级。入口: main.py -> from wecom_service import app

v2 变更:
  - echo 桩替换为 dianxiaoli_core.brain（双价报价/议价/下单/断货登记/付款截图→待核准/老板指令）
  - 挂载老板端控制台 /boss?key=BOSS_KEY 与自检 /status
  - 线程异常捕获: 报错翻译成人话写入 /status（如 60020 直接给出该加白的 IP）
"""
import threading

from fastapi import FastAPI

from wecom_core import build_router
import dianxiaoli_core as dx

app = FastAPI(title="dianxiaoli")


@app.get("/")
def health():
    return {"service": "店小力 AI 店员", "status": "ok",
            "console": "/boss?key=<BOSS_KEY>", "selfcheck": "/status"}


@app.get("/egress")
def egress():
    """返回 Railway 当前出口 IP——把它填进企微后台的可信 IP"""
    import requests
    try:
        ip = requests.get("https://api.ipify.org", timeout=6).text.strip()
        return {"railway_egress_ip": ip,
                "note": "把这个IP追加到: 店小力AI店员应用 → 企业可信IP（英文分号分隔）"}
    except Exception as e:
        return {"error": str(e)}


def guarded_brain(msg):
    try:
        return dx.brain(msg)
    except Exception as e:
        dx.record_error(e)
        raise


# 显式构建适配器，以便核准/驳回后主动回告顾客
_adapter = None
try:
    from wecom_core import WeComKfAdapter, OutboundReply
    from wecom_core import CursorStore
    _adapter = WeComKfAdapter(
        on_message=guarded_brain,
        cursor_store=CursorStore(str(dx.DATA_DIR / "wecom_cursor.json")),
    )

    def _notify(kfid, userid, text):
        if kfid and userid and text:
            _adapter.send(kfid, userid, OutboundReply(text=text))

    dx.set_notifier(_notify)
except Exception as _e:          # 环境变量缺失等情况下不阻断服务
    dx.record_error(_e)

app.include_router(build_router(on_message=guarded_brain, adapter=_adapter))
app.include_router(dx.router)

# 飞书多维表格看板同步 (环境变量配齐才生效)
import feishu_sync
app.include_router(feishu_sync.router)
feishu_sync.start_auto_sync()

# 后台线程(拉取/分发)异常 → 记录到 /status
_orig_hook = threading.excepthook


def _hook(args):
    if args.exc_value:
        dx.record_error(args.exc_value)
    _orig_hook(args)


threading.excepthook = _hook
