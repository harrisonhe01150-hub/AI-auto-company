# 企业微信·微信客服适配器 — 集成指南

**目标位置**: 你的仓库 `channels/wecom.py`（连同 `wecom_crypto.py`、`wecom_client.py`）
**离线测试**: `python wecom/test_wecom_local.py` — 12 项断言已全部通过（加解密回环/验签/翻页拉取/媒体注入/游标持久化）

## 1. 企微后台开通步骤（一次性，约 30 分钟）

1. 注册企业微信（个体户/企业均可），进入管理后台；
2. 开通「微信客服」：应用管理 → 微信客服 → 为商户创建一个客服账号（即"AI 店员号"），记下 `open_kfid`；
3. 微信客服 → API → 获取 `Secret`（即 `WECOM_KF_SECRET`）；
4. 配置回调：URL 填 `https://<你的Railway域名>/wecom/callback`，自定义 `Token` 与 `EncodingAESKey`（43 位随机串，后台可生成）；保存时企微会发 GET 验证——先部署再保存；
5. 在「企业可信 IP」中加入 Railway 出口 IP（企微 API 要求白名单）；
6. 生成客服账号二维码/链接：贴档口柜台、发老板客户群——顾客用**普通微信**扫码即入会话。

## 2. 环境变量（Railway）

```
WECOM_CORP_ID   = ww************      # 企业 CorpID（我的企业→企业信息）
WECOM_KF_SECRET = ***                 # 微信客服 Secret
WECOM_TOKEN     = ***                 # 回调 Token（第4步自定义）
WECOM_AES_KEY   = ***                 # 回调 EncodingAESKey（43位）
```

依赖：`pip install pycryptodome requests`（fastapi 你已有）。

## 3. 接入主程序（3 行）

```python
from channels.wecom import build_router

def sales_brain_entry(msg):          # msg: InboundMessage
    # → 路由到你的 RAG 销售大脑 / 库存 / OCR
    #   msg.msg_type == "image" 时 msg.media_bytes 即付款截图原始字节
    #   msg.text == "__EVENT_ENTER_SESSION__" 时返回欢迎语
    reply_text = core.handle(conversation_id=msg.conversation_id,
                             sender=msg.sender_id, text=msg.text,
                             media=msg.media_bytes, channel=msg.channel)
    return OutboundReply(text=reply_text) if reply_text else None

app.include_router(build_router(on_message=sales_brain_entry))
```

字段映射说明：`InboundMessage` 的命名是中性的（conversation_id / sender_id / account_id），
与你 WhatsApp 适配器的内部消息模型若有出入，只改 `wecom_adapter.py` 里
`_dispatch_one()` 的构造处即可，业务核心零改动。

## 4. 行为设计要点（已实现）

- **拉取模式**：微信客服不推消息正文，回调只报"有新消息"事件（含 Token），适配器自动
  `sync_msg` 游标翻页拉全量并逐条分发；游标持久化在 JSON 文件（单实例够用，换 Redis 只需替换 `CursorStore`）；
- **回调 5 秒约束**：POST 立即回 `success`，拉取分发走后台线程，避免企微重推造成重复；
- **防自我对话**：`origin=4`（客服侧发出）的消息自动跳过；
- **媒体桥接**：顾客图片自动下载为 bytes 注入 `media_bytes` → 直接喂 payment-handler OCR；
  回复可携带 `image_bytes`（商品图/对账单），适配器自动上传素材再发送；
- **长文本切分**：超 2000 字自动分段发送；
- **token 容错**：access_token 过期自动强刷重试一次。

## 5. 已知边界与下一步

- **主动触达限制**：微信客服对"客服主动发消息"有会话状态与时限约束（顾客近 48h 内有消息时
  可正常回复；超窗主动触达受限）。日报/到货通知等主动推送发给**老板端**不受此限
  （老板用企微 App 或后续 ClawBot 通道），发给**顾客**的超窗通知需走事件模板或引导顾客再次进会话——
  集训期实测后定策略；
- **语音消息**：kf 的 voice 为 amr 格式，接 ASR 前需转码（下一迭代）；
- **压测**：接通后跑 `scenarios_zh_wholesale_retail_v1.yaml` 中文场景集过 Agent C 门禁；
- **ClawBot spike**（Week 2 可选）：个人微信官方通道验证，与本适配器并行不冲突——
  渠道解耦架构下它只是又一个 adapter。
