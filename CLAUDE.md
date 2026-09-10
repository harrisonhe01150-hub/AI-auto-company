# 店小力（Dianxiaoli）· 项目说明

给在这个仓库里工作的 AI agent 看的。开工前读完，不要问已经写在这里的事。

## 这是什么

住在微信客服 / WhatsApp 里的 AI 店员：买家扫码问价、砍价、下单、发付款截图、问物流；老板一句大白话核单、上新、看日报。
定位是**给老板一个已经在干活的员工，不是卖工具**。

三家付费客户（杭州美辉 / Lifong Trading / Lolawe Fashions），三版产品：大众版（小程序，开发中）、定制版（现在的主力形态）、工厂版（FOB 阶梯报价、MOQ、打样、交期、PI）。

## 三条红线（架构级，不是提示词，任何改动不得绕过）

1. 不报库存数量
2. 不说别的客户拿什么价、不透露底价、不编价格（商品库外的价格 → 整条回复作废）
3. AI 永不确认发货、永不判断钱到没到 —— 人在环上

## 模型分工（写代码和写材料都别搞错）

| 用途 | 服务 | 位置 |
|---|---|---|
| 文字对话 | DeepSeek | `dianxiaoli_brain.py` |
| 认图（付款截图 / 商品照 / 规格图） | Claude vision（`VISION_MODEL`，默认 claude-sonnet-4-6，备用 haiku） | `dianxiaoli_brain.py` `VISION_PROMPT` |
| 语音转写 | OpenAI Whisper | `whatsapp_core/` |
| 社媒生图 / 修图 | GPT Image | **不在本仓库**，在 Lolawe 部署里 |

任一服务挂了 → 退回规则引擎，不许硬失败。

## 代码地图

- `dianxiaoli_core.py` — 大脑入口 `brain()`（conv_log 包装）→ `_brain_impl`；`_owner_brain`（老板指令）；图片分支（payment / product / spec / other）；工厂买家分支**在英文/外贸分支之前**；动作标记 `[ORDER:sku:qty]` `[WAITLIST]` `[ESCALATE]`；路由 `/boss`、`/boss/state|act|media|ask|stock|toggle`、`/boss/import`、`/risk/export`、`/audit/daily|weekly`、`/status`。鉴权 `?key=BOSS_KEY`。
- `dianxiaoli_brain.py` — LLM 提示词、`VISION_PROMPT`、价格幻觉守门。
- `factory_pack.py` — 工厂版：`DEFAULTS`（ports / fx 7.2 / fob_uplift 1.22 / moq_fob 500 / 阶梯折扣 / 打样费 / 交期）、`intent` `parse_qty` `parse_port` `fob_unit_usd` `quote` `handle_buyer` `owner_ship`；开关 `d["factory"]["enabled"]`。
- `catalog_import.py` — xlsx → `custom_skus` + `stock`；中英文表头、`FOB@N` 列、重复 SKU 后者覆盖、缺价推算（拿货=零售×0.7，零售=拿货×1.4）；`write_template`；CLI `--template/--replace`。
- `agent_c_audit.py` — 质检门禁 9 项检查 + 日报/周报；scheduler `AUDIT_DAILY_AT` / `AUDIT_WEEKLY_AT`，`AUDIT_SCHEDULER=0` 关。
- `conv_log.py` — JSONL per day，`classify_scenario`。
- `dianxiaoli_skills/` — 话术技能包：`skill_loader.py` 按 agent+scenario 选、priority、字符预算、按客户启停；`skills/agent_b/`（销售）、`skills/agent_a/`（社媒）。每个 SKILL.md frontmatter 必须有 `name/description/agent/scenarios/priority/max_chars/source`，且**必含「红线」段**，上线前过门禁。
- `wecom_service.py` / `wecom_core/` — 企微微信客服适配器，`/wecom/callback`、`/egress`。
- `whatsapp_core/` — WhatsApp + Whisper 语音。
- `feishu_sync.py` — **暂停**，环境变量不配即不启动，任何材料不再提。
- 模块目录：sales_brain、inventory_manager、payment_handler、report_engine、customer_memory、escalation_handler、restock_waitlist、image_sender、upsell_logic、stock_xlsx_importer。

六个内部 Agent：A 获客 / B 销售 / C 质检 / D 财务 / E 商务 / F 生产线。

## 测试（改完必跑，全绿才交）

```
testing/scenario_gate.py            28 中文场景
testing/scenario_gate_factory.py    26 工厂场景 f-001…f-051
testing/test_vision.py              41 图片
testing/test_audit.py               44 质检审计
testing/test_catalog_import.py      19 导入
dianxiaoli_skills/tests/test_skill_loader.py   21 技能包
testing/test_notify.py  test_wecom_core.py  test_cold_start.py  test_llm_brain.py
```

测试用 env：`WECOM_CORP_ID/KF_SECRET/TOKEN/AES_KEY`、`BOSS_KEY=k`、`LLM_ENABLED=0`、`AUDIT_SCHEDULER=0`、`DATA_DIR=<tmp>`。

## 环境变量

`DEEPSEEK_API_KEY/MODEL`、`ANTHROPIC_API_KEY`、`VISION_MODEL/MIN_CONF/FALLBACK`、`LLM_ENABLED/MAX_TOKENS`、`BOSS_KEY`、`DATA_DIR`（Railway 持久卷）、`WECOM_*`（含 `WECOM_COLD_START_SKIP`）、`AUDIT_*`、`OPENAI_API_KEY`。`FEISHU_*` 不配。

## 改代码的流程

1. 改 → 跑相关测试 + 全量门禁，**全绿才算完**
2. 清理 `conv_logs/ audit/ dianxiaoli_data.json dianxiaoli_media/ __pycache__/ gate_report*.json`
3. 写 CHANGELOG 条目：`## [vX.Y.Z] — YYYY-MM-DD` + Added / Changed / Fixed
4. 更新 `docs/ROADMAP.md` 的已完成表
5. commit 附 `Co-Authored-By:` 行

## 环境与坑

- 本机 Windows cmd。`findstr` 不是 `grep`，`dir` 不是 `ls`。
- 每次 Railway 部署后**出口 IP 会变** → 企微 errcode 60020 → 开 `/egress?key=` 取新 IP → 企微「微信客服 → 可信IP」加进去。故障先看 `/status`（`ai_on`、`last_error`）。
- 旧 `BOSS_KEY` 曾泄露，只能用轮换后的 key。绝不把任何 key 写进代码或文档。
- 根目录有三个 0 字节的垃圾文件 `cd` / `git` / `tar`（误敲命令留下的），可以删。
- `C:\Users\harri\OneDrive\one person company\模块库` 是本仓库的**旧副本**。永远改这里（`C:\Users\harri\code\AI-auto-company`），不要改那个。

## 明确不做

排产 / PDA / 跑图 / 一物一码。工厂没 ERP 的后续可加「工单状态板」（记账延伸，不是排产）。

## 写代码的风格：ponytail

本仓库装了 ponytail skill（`.claude/skills/ponytail/`）。默认按它的阶梯走：

1. 这需要存在吗（YAGNI）→ 2. 代码库里已经有了吗 → 3. 标准库能做吗 → 4. 平台原生能做吗 → 5. 已装依赖能做吗 → 6. 能一行吗 → 7. 才写最小实现

不砍：输入校验、防数据丢失的错误处理、安全、三条红线、明确要求的东西。
关掉：说「stop ponytail」或 `/ponytail off`。命令：`/ponytail-review`（看当前改动）、`/ponytail-audit`（全仓库过度设计审计）、`/ponytail-debt`（收集 `ponytail:` 注释）。

## 对话风格

说人话，不用术语。不编数字 —— 真实数字可以说得大胆，但不能造。我错的时候直接说不，给具体替代方案。
