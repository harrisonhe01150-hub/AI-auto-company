# 店小力技能包（dianxiaoli_skills）

一个零依赖的加载器 + 首批 11 个「义乌版」技能，给 Agent B（销售）和 Agent A（获客/内容）用。
技能是 SKILL.md 格式的纯文本，**模型无关**——DeepSeek 文本链直接吃。

```
dianxiaoli_skills/
├── skill_loader.py          # 加载器：按 Agent + 场景挑技能，拼成提示词片段
├── skills/
│   ├── agent_b/             # 销售 AI，6 个，运行时逐轮注入
│   └── agent_a/             # 内容策略 Agent，5 个，周任务注入
├── tests/test_skill_loader.py   # 21 条：解析 / 选择 / 预算 / 首批技能门禁
├── SOURCES.md               # 来源、锁定 commit、许可证
└── README.md
```

## 怎么接进 dianxiaoli_brain.py（三处改动）

**1. 启动时加载一次**

```python
from skill_loader import SkillRegistry
SKILLS = SkillRegistry.load(os.path.join(ROOT, "skills"))   # 加载失败会直接抛错，别 try/except 吞掉
```

**2. Agent B：在拼 system prompt 的地方，按场景追加**

```python
def build_system_prompt(scenario: str, client_cfg: dict) -> str:
    block = SKILLS.render("B", scenario, client_config=client_cfg)
    return SYSTEM_PROMPT + ("\n\n" + block if block else "")
```

`scenario` 直接用现有意图分类的结果，映射关系：

| 意图分类输出 | scenario |
|---|---|
| 询价 / 问货 | 首次询盘 |
| 议价 / 砍价 | 议价 |
| 异议（太贵、别家便宜、先看看、质量） | 异议 |
| 下单 | 下单 |
| 断货 | 断货 |
| 付款凭证 | 付款 |
| 投诉 | 投诉 |
| 闲聊 | 闲聊 |
| 其他 | 通用 |

标了「通用」的技能（转人工、对话分支、语气跟随）任何场景都会注入，转人工永远排第一。

**3. 每个客户的 config.json 可以开关技能（可选）**

```json
{ "skills_disabled": ["yiwu-cross-sell-upsell"] }
```
不写就是全部默认技能。

**Agent A** 不是逐轮注入，是周任务：周一跑 `render("A", "周复盘")` → `render("A", "周计划")`，
输入归因数据和库存，输出交给现有 ig auto 发布链。

## 三条不能破的规矩

1. **技能只管措辞，不管数字。** 价格、折扣、起批量、库存全部由代码给，技能正文里不允许出现任何具体价格。
2. **技能上线前过门禁。** 改任何一个 .md 都要重跑 `scenario_gate.py`（28 条）+ 本包的 21 条。技能写得再好，撞红线一律不上。
3. **来源锁 commit。** 见 SOURCES.md。上游仓库更新不自动跟，要升级就重新走一遍：拉 → 本地化 → 过门禁。

## 跑测试

```
cd dianxiaoli_skills
python -m unittest discover -s tests -v
python skill_loader.py --list                       # 看装了什么
python skill_loader.py --agent B --scenario 议价     # 预览注入文本
```

## 下一步（对应路线图序 3）

- 话术自学习：Agent C 每晚打标签 → 从成交对话抽例句 → 写进 `skills/agent_b/_learned/<client>/` 目录，
  加载器已经支持按客户开关，只需给学习产物一个独立的 scenario 标签。
- 工厂行业包（序 2）：新增 `skills/agent_b/factory-*.md`，场景标签用 `FOB报价 / 打样 / 交期`。
