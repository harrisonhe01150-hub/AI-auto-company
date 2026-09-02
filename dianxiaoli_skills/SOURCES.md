# 技能来源与版本锁定

所有上游仓库均为 MIT 许可。**没有一个技能是原样安装的**——只借骨架，正文全部改写为义乌批发零售 / 海外华商语境，并加入红线段落。
升级上游时必须重新走「拉 → 本地化 → 过门禁」，不允许自动更新。

| 本地技能 | 上游仓库 | 上游文件 | 锁定 commit | 借了什么 |
|---|---|---|---|---|
| yiwu-objection-handling | louisblythe/Sales-Skills | skills/objection-handling/SKILL.md | e0f13a6 | LAER 四步骨架 |
| yiwu-price-negotiation | louisblythe/Sales-Skills | skills/pricing-negotiation/SKILL.md | e0f13a6 | 不先让步 / 用阶梯换让步 / 超权升级 |
| yiwu-conversation-branching | louisblythe/Sales-Skills | skills/conversation-branching/SKILL.md | e0f13a6 | 「每条回复是决策点」架构 |
| yiwu-handoff-detection | louisblythe/Sales-Skills | skills/handoff-detection/SKILL.md | e0f13a6 | 主动转优于被动转、上下文跟着走 |
| yiwu-tone-matching | louisblythe/Sales-Skills | skills/tone-matching/SKILL.md | e0f13a6 | 跟随对方语气的原则 |
| yiwu-cross-sell-upsell | louisblythe/Sales-Skills | skills/cross-sell-upsell-detection/SKILL.md | e0f13a6 | 时机判断 |
| shop-brand-voice-extractor | replynodes/awesome-social-media-skills | skills/content-strategy/brand-voice-extractor.md | 0c55ace | 输入-做法-输出-验收结构 |
| shop-weekly-content-planner | replynodes/awesome-social-media-skills | skills/content-strategy/content-calendar-planner.md | 0c55ace | 排期逻辑、支柱配比 |
| shop-carousel-designer | replynodes/awesome-social-media-skills | skills/images/carousel-content-designer.md | 0c55ace | 首尾页职责、一页一点 |
| shop-multi-platform-adapter | replynodes/awesome-social-media-skills | skills/repurposing/multi-platform-content-adapter.md | 0c55ace | 一源多版、平台差异表 |
| shop-weekly-performance-review | replynodes/awesome-social-media-skills | skills/analytics/weekly-content-review-agent.md | 0c55ace | 周复盘结构 |

审查记录（2026-09-02）：四个仓库共 378 个 SKILL.md，可执行脚本仅 1 个（awesome-social-media-skills/scripts/generate_index.py，索引生成器，未引入）；
目标文件中未发现提示词注入类文本。coreyhaines31/marketingskills（d4ff28a）与 OneWave-AI/claude-skills（82859c0）已拉取备查，首批未采用。
