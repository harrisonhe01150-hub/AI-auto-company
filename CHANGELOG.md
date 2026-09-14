# 模块库 — CHANGELOG

All notable changes to the warehouse are recorded here. Each module also
carries its own `__version__` string inside `module.py`.

Format: `## [warehouse-version] — YYYY-MM-DD`
within each entry, modules are grouped by `Added` / `Changed` / `Deprecated` / `Fixed`.

---

## [v1.7.4] — 2026-09-14

### 老板通知第二通道：企微应用消息（路线图序 10e）

v1.7.3 的群机器人要先建群，而度小满线的国内客户大多本来就在企业微信体系里。
老板在企微后台建个自建应用，填 `WECOM_AGENT_ID` / `WECOM_AGENT_SECRET` / `BOSS_WECOM_USERID`
三个变量，新单就直接进他自己的企微聊天框：不用建群、没有 48 小时限制、一对一。
两条正式通道并行，微信客服只当最后兜底。三个变量不填时行为与 v1.7.3 完全一致。

#### Added
- `boss_notify.py` — 应用消息通道。`app_configured()`（三个变量齐了才算配好）、
  `channels()`（返回已配好的正式通道，如 `["webhook", "app"]`）、`CHANNEL_CN`（通道中文名）。
  `_app_client()` 是模块级懒加载单例的 `wecom_core.client.WeComClient(corp_id=WECOM_CORP_ID,
  secret=WECOM_AGENT_SECRET)`——单例才有 access_token 缓存和 40014/42001 自动重取。
  `_via_app()` 调 `message/send`，payload `{touser, msgtype:"markdown", agentid:int, markdown:{content}}`；
  markdown 只在企微客户端渲染，老板用个人微信互通登录看到的是纯文本（文案本来就纯文本友好）。
  返回里 `invaliduser` 非空算部分失败：「这些人没收到：{userid}，检查 BOSS_WECOM_USERID 是不是
  通讯录里的成员账号」。`WeComAPIError` 走 `_why_human` 的口径：60020 → 打开 `/egress` 加可信 IP，
  40013/40014/42001 → 去 Railway 检查 `WECOM_AGENT_SECRET`，其余 → 报错误码让技术查。
- `docs/BOSS_NOTIFY.md` — 新增一节「另一种方式：直接发到您的企业微信（不用建群）」：
  建自建应用 → 记 AgentId / Secret → 可见范围加上老板 → 通讯录里抄「账号」当 userid →
  Railway 填三个变量 → 重新部署；两种可同时开，个人微信互通也能收；
  收不到时看 `/status` 的 `boss_notify.channels` 和 `last.why`。
- `testing/test_boss_notify.py` — 新增 43 项（共 99）：三变量缺一不成、`channels()` 四种组合、
  只配 app 时 `message/send` 的 touser/agentid/markdown、两条都配时两边都收到、
  一条挂了另一条照送、两条全挂回退微信客服、三条全断的留痕、`invaliduser`、
  60020 与 40013 的人话、限流下第 19 条 app 照发而群里那份进 backlog、`/status.channels`、帮助文案。

#### Changed
- `boss_notify.py` `configured()` 语义改为「至少一条正式通道已配」（`BOSS_WEBHOOK` 或应用消息三件套）。
  投递策略改为：已配好的正式通道**每条都发**、互不影响，全都没发成（或一条都没配）才回退微信客服；
  `PUSH_LOG` 的 `via` 记实际成功的通道（`"webhook+app"` / `"app"` / `"kf"` / `"none"`），
  `ok` = 至少一条正式通道成功或微信客服成功，`why` 把各通道的失败原因用「；」拼起来（每条 ≤ 60 字）。
  60 秒 18 条的限流只对群机器人计数（那是群机器人的规矩），应用消息不限；
  被限流压下时若 app 已配，app 照发、只有群里那份进 backlog，backlog 因此改成按通道存
  （`_backlog_webhook`，`_backlog` 保留为同一个列表的别名）。`flush()` 不变。
- `dianxiaoli_core.py` — `/status` 的 `boss_notify` 段新增 `channels`（`configured` 保留）；
  老板帮助文案按已开通道显示「📣 通知：新单已自动推送（群机器人 + 应用消息）」，
  只开群机器人时仍是 v1.7.3 那句「📣 通知：新单已自动推群」，一条都没配时文案不变。

#### 回归
- 中文门禁 28/28 · 工厂门禁 26/26 · audit 44 · catalog_import 24 · llm_brain 23 · notify 17 ·
  vision 41 · wecom_core 12 · demo_catalog 42 · cold_start 6 · skill_loader OK ·
  boss_notify 99（56 → 99，新增 43）—— 全绿。

---

## [v1.7.3] — 2026-09-13

### 老板通知：新单推群（路线图序 10d）

线上真实漏单：买家下单 / 转人工 / 发付款截图，`add_pending()` 只写台账和 conv_log，
老板收不到任何动静，只能自己开网页或对着 AI 说「待办」。
老板拉个企微群、加群机器人、把 webhook 填进 `BOSS_WEBHOOK`，从此每笔新单立刻推到群里。
没配这个变量时行为与 v1.7.2 完全一致（退回原来的微信客服通道）。

#### Added
- `boss_notify.py`（新）— 老板推送通道。**推送在后台线程发，不拖慢买家回复**：
  `push()` 只做入队和限流判断就返回（返回值＝受理了没有，发成没发成看 `PUSH_LOG`），
  真正的网络请求交给一个 daemon 线程；企微慢 6 秒也不会让买家跟着等。
  `PUSH_LOG` / `_WINDOW` / `_backlog` / 在飞线程名单统一由一把 `threading.Lock` 看着；
  `flush(timeout)` 等在飞的发完（测试和收尾用）。`configured()`；投递先发企微群机器人
  markdown（timeout 6s，HTTP 200 且 `errcode==0` 才算成功），发不出去就退回
  `core.NOTIFIER(kfid, boss_userid, text)`；任何异常吞掉并记进 `record_error`，永不影响接待。
  模块级 `PUSH_LOG` 留最近 20 条（ts / kind / ok / via / why / 正文前 60 字）。
  限流按企微规矩 20 条/分钟做：60 秒滑动窗口，超过 18 条的压进 `_backlog`，
  下次有空位时并成一条「⏳ 积压 N 条」发出（最多列 10 行）；限流本身不起常驻线程。
  文案 `fmt_pending()`（🧾 新单待核准 / 💳 付款截图待核对 / ⚠️ 金额对不上 / 🙋 买家要找老板 / 📌 待处理，
  金额为 0 不显示金额，desc 超 60 字截断）和 `fmt_notify_failed()`（把发不出去的原话原样给老板，让他手动补发）。
  失败原因按错误码翻成下一步该干什么（口径同 `record_error` 的 hint）：40013/40014/42001 → 去 Railway
  检查 `WECOM_CORP_ID` / `WECOM_KF_SECRET`；60020 → 打开 `/egress` 把新 IP 加进企微可信 IP；
  95000 → 客服账号不在「通过 API 管理」名单；其余 95xxx 或明写 48 小时 / `not allowed to send`
  → 「买家超过 48 小时没说话」；再其他 → 「微信客服没发出去（错误码 NNNNN）」。
- `dianxiaoli_core.py` — `add_pending()` 末尾推一条给老板（用内存里的待办，不等 `save(d)`）；
  `_notify()` 三条失败路径（通道没注入 / 缺路由 / 发送抛异常）统一推一条「📵 没能回告买家」；
  `/status` 新增 `boss_notify` 段（`configured` / `sent` / `failed` / `last`）；
  老板帮助文案按配没配显示「📣 通知：新单已自动推群」或引导去看 docs/BOSS_NOTIFY.md。
- `docs/BOSS_NOTIFY.md`（新）— 给老板看的三步配置说明：建群加机器人 → 填 `BOSS_WEBHOOK` → 重新部署，
  会收到哪几种消息、收不到时怎么用 `/status` 自查，并提醒 webhook 地址等于钥匙别外传。
- `testing/test_boss_notify.py` — 56 项：两条通道与回退、POST 的 url/msgtype/正文/超时、
  HTTP 500 与 errcode≠0 与网络异常、限流 25 条只出 18 条 + 积压合并、
  企微慢 2 秒时买家回话仍 < 0.5 秒出来、五种待办文案、48 小时与各类企微错误码的人话映射、
  买家下单到推送的真实链路、回告失败推送、`/status`、帮助两种状态、I-07 / I-08。

#### Changed
- `agent_c_audit.py` `push_to_boss()` — 改为走 `boss_notify.push(..., kind="audit")`，
  日报/周报同样优先进群；没配 webhook 时行为不变。日报/周报不在买家等回话的链路上
  （定时任务或老板自己点的），所以这里 `flush()` 等发完再回报成没成，返回值语义与以前一致。

#### Fixed
- I-07：`find_item` 的单字关键词（线/壶/灯/裙/袜/伞）会把「在线吗」「上线了」当成问数据线。
  改为单字关键词紧跟在汉字后面时，整句得带问货信号（多少/价/钱/要/来/件…）才算数；
  「伞多少钱」「这条裙子有 M 码吗？多少钱？」照旧命中，`_ITEM_KW` 表本身没动。
- I-08：幻觉红线那句在找不到相近款时回「可以说下商品类目，我帮您找相近的现货」，等于搪塞。
  改为列出在售前 3 款（口径与 v1.7.2 买家侧「这款我们这里没有」一致），目录为空时说老板马上补。

#### 回归
- 中文门禁 28/28 · 工厂门禁 26/26 · audit 44 · catalog_import 24 · llm_brain 23 · notify 17 ·
  vision 41 · wecom_core 12 · demo_catalog 42 · cold_start 6 · skill_loader OK ·
  boss_notify 56（新增）—— 全绿。

---

## [v1.7.2] — 2026-09-10

### 演示商品可关闭（路线图序 10b）

真实客户店里，内置的 A3 保温壶等 14 款演示商品不能再被报价。老板一句「关闭演示商品」，
店里就只剩他自己上新 / 导表的商品；说「开启演示商品」随时恢复。默认仍是开启，演示店和现有测试行为不变。

#### Added
- `dianxiaoli_core.py` `demo_catalog`（存档字段，缺省视为开启）+ `demo_on(d)` helper；
  `get_catalog(d)` 在关闭时只返回 `custom_skus`（顺序保持）。
- 老板指令：「关闭/关掉/隐藏/停用/取消 + 演示商品/演示目录/示例商品」→ 隐藏，回话报出还剩几款自有商品；
  一款都没有时改为提示先上传产品表或说「上新」。「开启/打开/显示/恢复」→ 恢复显示。帮助文案新增
  `🧪 演示：关闭演示商品 / 开启演示商品`。
- 买家侧「这款我们这里没有」：目录里查无此货号 / 此品类时，直接说没有并列出在售前 3 款，
  不再用「哪一款？说个货号」搪塞；店里一款商品都没有时告诉买家老板马上补。
- 买家兜底那句的举例改成按当前目录动态取第一款（原先写死「A3」「保温壶」，真实客户店的买家会照着问演示品）；
  目录为空时改说「店里商品马上上架，稍等老板一下。」。
- 老板网页 AI 开关旁新增「演示商品：显示中 / 已隐藏」按钮（`#demoBtn` → `POST /boss/demo_toggle`）；
  `/boss/state` 与 `/status` 返回 `demo_catalog`。
- `testing/test_demo_catalog.py` — 42 项：默认值与旧存档兼容、目录裁剪与顺序、老板四种措辞、
  买家「没有这款」与空店文案、demo 开启时的报价回归、端点鉴权与翻转、网页按钮、导表摘要提示。

#### Changed
- `catalog_import.py` `summary_text(..., demo_on=True)` — 导表成功后追加一句
  「提示：内置演示商品还在显示中，正式接待前对我说「关闭演示商品」。」；`/boss/import` 按当前开关传入。
- `dianxiaoli_core.py` — `find_item` 的关键词表提为模块级 `_ITEM_KW`（买家侧「没有这款」判断复用），
  匹配逻辑未变。

#### Fixed
- `/boss/state` 的库存表原先直接遍历内置 `CATALOG`，老板导表/上新的商品在网页库存表里根本不显示；
  改为遍历 `get_catalog(d)`，自有商品现在能看能改。

#### 回归
- 中文门禁 28/28 · 工厂门禁 26/26 · audit 44 · catalog_import 24 · llm_brain 23 · notify 17 ·
  vision 41 · wecom_core 12 · cold_start 6 · skill_loader OK · demo_catalog 42（新增）—— 全绿。

---

## [v1.7.1] — 2026-09-09

### 老板端自己传产品表（路线图序 10）

#### Added
- `dianxiaoli_core.py` `BOSS_HTML` — 库存下面新增「📄 产品表」区：两个模板下载链接（档口版 / 工厂版，key 由 JS 按当前 URL 拼）、
  `.xlsx` 选择框、「先清空上次上传的产品」勾选（走 `?replace=1`，只清 `custom_skus`，内置演示商品不动）、「上传导入」按钮。
  上传走 multipart FormData（不走 JSON `api()`），成功后把端点返回的 `text` 摘要贴进 `#importResult`、清空文件框（防手滑重传）并自动
  `refresh()` 刷库存表；失败按人话提示，上传期间按钮置灰。
  从此老板不用找人跑 CLI，自己在手机上就能换目录。
- `docs/DEPLOY_STATIC_IP.md` — 企微可信 IP 为什么每次部署都失效（Railway 出口 IP 会变）、
  当前手工救火流程（`/egress?key=` → 企微微信客服 → 可信 IP）、以及开固定出口 IP 的分步做法。
- `docs/ROADMAP.md` — 序 10 移入「已完成」；队列新增 10b「基础演示目录可关闭」（本版不实现）。

#### Changed
- `testing/test_catalog_import.py` — 19 项 → 24 项：老板端上传区标记齐全、`/boss` 无 key 401、
  模板链接由 JS 带 key 且分 `factory=0/1`、上传用 FormData + `replace=1`、UI 显示的确实是端点返回的 `text` 字段。

#### 回归
- catalog_import 24 · 中文门禁 28 · 工厂门禁 26 · vision 41 · notify 17 · cold_start 6 · wecom_core 12 · llm_brain 23 · audit 44 · skills 21 —— 全绿。

---

## [v1.7.0] — 2026-09-04

### 产品表导入（路线图 2.6 演示店 / 新客户交付第一步）

#### Added
- `catalog_import.py` — 一张 xlsx → `custom_skus` + `stock`：中英文表头都认、列顺序随意、`FOB@数量` 列直接进 SKU 的 `fob_tiers`，
  缺拿货价按零售七折推、缺零售按拿货 1.4 倍推、重复货号后者覆盖并提示、无价格行跳过并提示。
- 端点 `POST /boss/import?key=…&replace=1`（multipart 字段 `file`）、`GET /boss/import/template?key=…&factory=0|1`（给客户填的空模板，含填写说明页）。
- CLI：`python catalog_import.py 产品表.xlsx` / `--template 模板.xlsx` / `--replace`。
- `testing/test_catalog_import.py` — 19 项全链路：解析 → 入库 → 买家真的能问到价 / FOB 阶梯真的被用上 / 端点鉴权 / 模板自己能被导回。
- `requirements.txt` 加 `openpyxl`。

---

## [v1.6.0] — 2026-09-04

### 工厂行业包 v1（路线图序 2）

#### Added
- `factory_pack.py` — FOB 阶梯报价矩阵（数量档 × 港口 × 币种，SKU 可自带 fob_tiers）、MOQ 判定、打样流程（待办「样品单」）、
  交期按数量档、报价单/PI 草稿（待办「报价单」）、买家物流查询从台账答、**老板「17 发货 SF…」→ 台账记单号 + 自动推给买家**。
  全部确定性；`d["factory"]["enabled"]` 开关，老板一句「开启工厂模式」即开。
- 规格图分流：`VISION_PROMPT` 新增 `spec` 类型；core 图片分支第四路 → 待办「转工程」，不报价。
- `dianxiaoli_skills/skills/agent_b/factory-*.md` — 外贸询盘 / 打样 / 交期 三个技能包（场景：外贸 / 打样 / 交期）。
- `testing/scenario_gate_factory.py` — 工厂门禁 26 条（含关掉时对档口零影响 2 条）。
- `docs/ROADMAP.md` — 产出路线 v3。

#### Changed
- `dianxiaoli_core.py` — 订单记录带 kfid / name / lang（发货回告要用）；`_owner_brain` 前置发货指令与工厂模式开关；
  英文询盘会话记 lang=en。`conv_log` 场景分类新增「打样」「交期」。

#### 回归
- 中文门禁 28/28 · 工厂门禁 26/26 · vision 41 · notify 17 · cold_start 6 · wecom_core 12 · llm_brain 23 · audit 44 · skills 21 —— 218 项全绿。

---

## [v1.5.0] — 2026-09-03

### Agent C 升级：从「上线前门禁」到「上线后每日审计」

#### Added
- `conv_log.py` — 对话留痕：按天 JSONL（`DATA_DIR/conv_logs/`），记录 in/out/pending/act/notify 六类事件；
  确定性场景分类（首次询盘/议价/下单/付款/投诉/转人工/外贸/图片/闲聊/老板指令），与技能包场景名对齐。
- `agent_c_audit.py` — 每日全量审计 + 周报：9 项确定性检查（库存数字泄露、目录外单价、底价/成本泄露、
  他人价格泄露、AI 擅自确认发货、行为承诺缺失、语言未跟随、未回复、回告通知失败）；
  结果标签（成交/待核准/转人工/流失/闲聊）取自台账事件，零人工标注；
  默认 23:30 出日报、周一 08:30 出周报，推老板微信；`/audit/daily` `/audit/weekly` 端点；`/status` 暴露审计状态。
- `dianxiaoli_skills/` — 技能包加载器 + 首批 11 个义乌版技能（Agent B 6 个 / Agent A 5 个），21 条测试。
- `testing/test_audit.py` — 44 条：每类缺陷埋雷必抓、真实规则引擎零误报、事件链路、周报聚合、端点、
  两个变异测试（改坏库存保密 / 改坏付款回复 → 审计在真实流量上抓到）。

#### Changed
- `dianxiaoli_core.py` — `brain()` 外层包一层留痕（原逻辑移入 `_brain_impl`，行为不变）；
  `add_pending` / `_do_act` / `_notify` 写事件；`/risk/export` 的 quality_assurance 改为真实门禁数与审计状态。
- `wecom_service.py` — 启动审计调度线程（`AUDIT_SCHEDULER=0` 可关；`AUDIT_DAILY_AT` / `AUDIT_WEEKLY_AT` 可调）。

#### 回归
- scenario_gate 28/28 · test_vision 41 · test_notify 17 · test_cold_start 6 · test_wecom_core 12 · test_llm_brain 23 · test_audit 44 · skills 21 —— 全绿。

---

## [v1.4.0] — 2026-06-11

### Harvested from Lolawe (Client #2) — first real warehouse compounding cycle

New policy (Harrison, 2026-06-11): **every bug fixed must leave behind (1) a
behaviour test that would have caught it and (2) a guard inside the module.
Bugs are only allowed to happen once.**

#### Added
- `customer_memory` v0.2.0 — conversation history: `update_from_reply()` now
  records each exchange; new `get_recent_history(phone, n)` returns LLM-ready
  messages. Fixes "bot forgets the previous message" (multi-turn context).
- `testing/lolawe_e2e_reference.py` — reference behaviour-test harness (mini
  Agent C): boots the real module stack with Meta API + LLM mocked, replays
  scripted conversations, asserts on behaviour. Adapt per client; run in
  GitHub Actions on every push.

#### Fixed / Hardened (boundary guards — catch integration bugs loudly)
- `whatsapp_core` v0.1.1 — `send_text` / `send_image_url` / `send_document`
  now REJECT a recipient that isn't a phone number (catches swapped-argument
  bugs that previously no-oped silently).
- `image_sender` v0.2.0 — same recipient guard on `send_for_skus` /
  `send_for_text`. (Root cause: Lolawe app.py called `send_for_text(reply,
  phone)` — args swapped — photos silently never sent for 2 weeks.)
- `inventory_manager` v0.1.1 — `init()` validates callback arity via
  `inspect.signature`; wrong `on_low_stock`/`on_restock` signature now fails
  FAST at boot instead of crashing silently in a thread at runtime.
- `escalation_handler` v0.1.1 — ASCII keywords now match on word boundaries
  ("sale" no longer hijacks "wholesale"); non-ASCII (Amharic etc.) keep
  substring matching.

---

## [v1.3.0] — 2026-06-07

### Graduated from Lifong main repo

Auto-generated by `scripts/graduate_modules.py`.

- `restock_waitlist` — module.py

---

## [v1.2] — 2026-06-07

### 🚨 Fixed — OneDrive sync truncation (3 modules)

A drift audit on 2026-06-07 revealed that three module.py files in the
warehouse copy had been silently truncated by OneDrive's mid-write sync at
v1.1 release time, leaving them unusable for client onboarding:

| Module | Truncated at | Lost lines |
|---|---|---|
| `payment_handler/module.py` | mid-line `r` (line 325) | 20 lines (rest of `reject()` + `get_pending()` + `list_pending()` + `init()`) |
| `restock_waitlist/module.py` | `def init(config: dict) -> Resto` (line 256) | 7 lines (`init()` factory) |
| `upsell_logic/module.py` | `conf` (line 226) | 2 lines (`init()` factory) |

Without `init()` factories, the modules were unusable — clients picking them
out of the warehouse couldn't instantiate.

Files restored from the Lifong main-repo source of truth (which was never
truncated — only the OneDrive-synced copy suffered).

This validates Dead Order #3 ("OneDrive sync may produce conflicting versions
— one truncated, one with duplicated content"). Files now restored via single
atomic Write per Dead Order #3 guidance.

A future `scripts/verify_warehouse.py` (Task #69) will run `ast.parse` on
every module.py + a pre-commit hook to prevent silent truncation from
shipping again.

### Changed — sales_brain hybrid LLM (graduate from Lifong)

`sales_brain` now supports `deepseek_api_key` + `deepseek_model` config
fields. When `deepseek_api_key` (or env var `DEEPSEEK_API_KEY`) is present,
the module routes calls through `LLMRouter` (DeepSeek primary, Claude
fallback) — ~13× cost reduction with no quality drop on typical sales
queries. Falls back gracefully to direct Anthropic when `LLMRouter` is not
on the host's Python path (standalone module use).

This change graduates the hybrid LLM optimisation that's been validated in
Lifong production since 2026-06-04. Backward compatible — existing
config.json files without `deepseek_api_key` keep working with Claude only.

Module-internal version `__version__` stays at `0.1.0` because the public
API is unchanged — only the LLM routing implementation evolved.

---

## [v1.1] — 2026-05-27 (same-day follow-up)

### Added — 2 reserved-but-built modules at v0.1.0

Both modules are skeletons — no live client deployed yet but the code is
production-ready for the first opt-in.

| Module | What it does |
|---|---|
| `payment_handler` | EFT payment-proof workflow: vision extract → dedup check → PAY-XXX ID → manager approve/reject command → multilingual customer confirmation |
| `upsell_logic` | Rule-based bundles + history-based co-occurrence + combined ranked suggestion + multilingual format |

Both follow the established patterns from v1.0 (init factory + JSON config +
env-var expansion + callable injection for dependencies + smoke-test verified).

⚠️ **Note (added retroactively in v1.2):** these v1.1 published files were
later discovered to be truncated by OneDrive sync. Fixed in v1.2.

### Updated

- `INDEX.md`: payment_handler moved under new 💰 Money category;
  upsell_logic moved under new 🛒 Cross-sell category; reserved list shrunk
  to just the two "concept only" entries (tiktok_auto_reply, instagram_auto_reply).

---

## [v1.0] — 2026-05-27

**Initial published warehouse**. Born from the live Lifong Trading deployment
that has been running 24/7 since 2025. All 9 modules extracted from the
production codebase, refactored to take client-specific data through config
injection (JSON + env vars) so they're reusable across any wholesale / retail
WhatsApp business.

### Added — 9 modules at v0.1.0

| Module | Source in Lifong | Lines (module.py) |
|---|---|---|
| `whatsapp_core` | `src/messaging.py` (full extract + shim) | ~230 |
| `sales_brain` | `src/agent_brain.py` (composable framework; Lifong reference impl stays) | ~180 |
| `escalation_handler` | `agent_brain.py` HARD_ESCALATE block | ~80 |
| `customer_memory` | `src/customer_routing.py` profile fns | ~180 |
| `image_sender` | `webhook_router.py` `_find_all_skus_in_text` + `messaging.send_meta_image_url` | ~150 |
| `inventory_manager` | `src/stock_commands.py` (core sold/received/balance/undo) | ~240 |
| `stock_xlsx_importer` | `src/stock_xlsx_importer.py` (full extract + shim) | ~250 |
| `restock_waitlist` | `src/restock_waitlist.py` (full extract + shim) | ~210 |
| `report_engine` | New minimal core; complex Lifong PDFs stay in `agent_d_report.py` | ~150 |

### Added — 5 warehouse-level documents

- `README.md` — friendly intro + usage example + roadmap
- `INDEX.md` — modules grouped by category with composition recipes
- `INVENTORY.md` — source mapping (which Lifong code became which module)
- `SPEC.md` — module developer contract (init / config / shims)
- `EXAMPLES.md` — Python wire-up snippets for common client setups
- `CHANGELOG.md` — this file

### Extraction patterns used (3 categories)

1. **Full extract + backward-compat shim** (3 modules):
   `restock_waitlist`, `stock_xlsx_importer`, `whatsapp_core`.
   Lifong's `src/<file>.py` is now a thin shim that loads the module +
   Lifong's client config + re-exports the original function names so all
   existing callers (webhook_router / customer_routing / etc) keep working
   without edits.

2. **Standalone module** (3 modules):
   `escalation_handler`, `image_sender`, `customer_memory`.
   The logic was inline in larger files (agent_brain / webhook_router /
   customer_routing). The module lives parallel to that inline code. No
   shim needed; future refactor will replace inline calls with module calls.

3. **Framework + Lifong reference implementation stays** (3 modules):
   `inventory_manager`, `report_engine`, `sales_brain`.
   The Lifong production files (`stock_commands.py`, `agent_d_report.py`,
   `agent_brain.py`) contain so much Lifong-specific business logic that a
   clean drop-in shim wasn't safe to do in the extraction session. The
   module is the composable framework / pluggable foundation; Lifong's
   existing files stay as the "advanced reference implementation" that
   demonstrates how the module composes for the most complex case. Future
   focused sessions will migrate Lifong onto the module.

### Reserved (deferred per business decision)

- `payment_handler` — Lifong opted out 2026-05-27 (anti-scam; Yoyo manually
  handles all EFT confirmations). Build per-client when a future client
  explicitly wants payment-proof OCR + manager approve/reject (~3-4 hours).
- `upsell_logic` — No client has asked yet.
- `tiktok_auto_reply`, `instagram_auto_reply` — Agent A platform tunnels
  in development; will mature into modules when stable.

### Validation

All 9 modules passed smoke tests during extraction:

- `restock_waitlist`: dedup + 4-lang notify + opt-out
- `stock_xlsx_importer`: parsed real 工作簿3.xlsx → 58 valid + 595 expected errors
- `escalation_handler`: keyword triggers verified
- `image_sender`: 3 SKUs detected + cap respected + hyphen-tolerant
- `customer_memory`: save / load / update_from_reply + context builder
- `whatsapp_core`: shim functions exposed correctly (full integration tested via Lifong production)
- `inventory_manager`: received / sold / undo + low-stock callback
- `report_engine`: 1860-byte PDF + senders proxy
- `sales_brain`: framework loads cleanly (full integration deferred to Lifong refactor session)

---

## Upcoming (planned)

### [v1.3] — Lifong Graph API migration (Task #67)

When Lifong's Meta-verified Business unlocks Instagram Graph API access (in
review now), the following modules will be added to the warehouse:

- `instagram_publisher_official` — Meta Graph API content publish (preferred)
- `instagram_publisher_playwright` — abstracted from Lifong's `agent_a_ad_engine.py` (fallback for clients without Meta verification)
- `instagram_dm_autoreply` — Instagram Messaging API (within 24h customer reply window)
- `instagram_comment_autoreply` — Graph API comment hook
- `instagram_insights` — weekly performance report via Graph API

### [v1.4] — Automated graduation (Task #69)

`scripts/graduate_modules.py` will run weekly (Windows Task Scheduler /
Railway cron) to:

1. Diff `lifong-ai-system/modules/` against the warehouse copy
2. Run `ast.parse` on every module.py to detect truncation
3. Auto-copy diffs, bump warehouse `__version__`, write CHANGELOG entry
4. Git commit + push warehouse repo
5. Report via Harrison's WhatsApp

This prevents silent drift (like v1.1 → v1.2 took 10 days to discover).

### [v2.0] — when first non-Lifong client deploys

Warehouse versioning becomes more disciplined: every module bumps its own
`__version__` for breaking changes; clients pin module versions in their
config.
