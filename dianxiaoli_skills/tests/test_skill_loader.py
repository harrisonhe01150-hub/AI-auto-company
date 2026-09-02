# -*- coding: utf-8 -*-
"""
技能包加载器测试 + 首批技能包门禁。
运行：python -m unittest discover -s tests -v   （在 dianxiaoli_skills 目录下）
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from skill_loader import SkillRegistry, SkillError, load_skill_file, parse_frontmatter  # noqa: E402

SKILLS_DIR = os.path.join(ROOT, "skills")

RED_LINE_WORDS = ("库存数", "底价", "别的客户", "别人拿货价", "他人拿货价", "客户成交价", "个人信息")


def _write(dirpath, name, text):
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def _skill(name="t-one", agent="B", scenarios="[议价]", priority=50, body="正文。", extra=""):
    return f"""---
name: {name}
description: 测试
agent: {agent}
scenarios: {scenarios}
priority: {priority}
{extra}---
{body}
"""


class TestFrontmatter(unittest.TestCase):
    def test_parse_scalars_lists_and_nested(self):
        meta, body = parse_frontmatter(
            "---\nname: a-b\nenabled: false\npriority: 7\nscenarios: [议价, 通用]\n"
            "source:\n  repo: x/y\n  adapted: true\n---\nhello"
        )
        self.assertEqual(meta["name"], "a-b")
        self.assertIs(meta["enabled"], False)
        self.assertEqual(meta["priority"], 7)
        self.assertEqual(meta["scenarios"], ["议价", "通用"])
        self.assertEqual(meta["source"], {"repo": "x/y", "adapted": True})
        self.assertEqual(body, "hello")

    def test_missing_frontmatter_raises(self):
        with self.assertRaises(SkillError):
            parse_frontmatter("no frontmatter here")


class TestLoadValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_bad_agent(self):
        p = _write(self.tmp, "a.md", _skill(agent="Z"))
        with self.assertRaises(SkillError):
            load_skill_file(p)

    def test_bad_name(self):
        p = _write(self.tmp, "a.md", _skill(name="Bad Name"))
        with self.assertRaises(SkillError):
            load_skill_file(p)

    def test_too_long_body(self):
        p = _write(self.tmp, "a.md", _skill(body="字" * 50, extra="max_chars: 10\n"))
        with self.assertRaises(SkillError):
            load_skill_file(p)

    def test_duplicate_name_rejected(self):
        _write(self.tmp, "a.md", _skill(name="dup"))
        _write(self.tmp, "b.md", _skill(name="dup"))
        with self.assertRaises(SkillError):
            SkillRegistry.load(self.tmp)

    def test_readme_ignored(self):
        _write(self.tmp, "README.md", "# not a skill")
        _write(self.tmp, "a.md", _skill())
        self.assertEqual(len(SkillRegistry.load(self.tmp)), 1)


class TestSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _write(self.tmp, "1.md", _skill("b-generic", "B", "[通用]", 60, "通用技能"))
        _write(self.tmp, "2.md", _skill("b-bargain", "B", "[议价]", 90, "议价技能"))
        _write(self.tmp, "3.md", _skill("b-order", "B", "[下单]", 40, "下单技能"))
        _write(self.tmp, "4.md", _skill("a-plan", "A", "[通用]", 95, "A 的技能"))
        _write(self.tmp, "5.md", _skill("b-off", "B", "[议价]", 99, "关掉的", extra="enabled: false\n"))
        self.reg = SkillRegistry.load(self.tmp)

    def test_scenario_and_generic(self):
        names = [s.name for s in self.reg.select("B", "议价")]
        self.assertEqual(names, ["b-bargain", "b-generic"])  # priority 降序，通用也在

    def test_agent_isolation(self):
        self.assertNotIn("a-plan", [s.name for s in self.reg.select("B", "通用")])
        self.assertEqual([s.name for s in self.reg.select("A", "周计划")], ["a-plan"])

    def test_disabled_skipped(self):
        self.assertNotIn("b-off", [s.name for s in self.reg.select("B", "议价")])

    def test_client_enable_disable(self):
        only = self.reg.select("B", "议价", client_enabled={"b-generic"})
        self.assertEqual([s.name for s in only], ["b-generic"])
        without = self.reg.select("B", "议价", client_disabled={"b-bargain"})
        self.assertEqual([s.name for s in without], ["b-generic"])

    def test_budget_keeps_high_priority(self):
        # 预算只够一个：留优先级最高的
        picked = self.reg.select("B", "议价", budget_chars=4)
        self.assertEqual([s.name for s in picked], ["b-bargain"])

    def test_deterministic_order(self):
        a = [s.name for s in self.reg.select("B", "议价")]
        b = [s.name for s in self.reg.select("B", "议价")]
        self.assertEqual(a, b)

    def test_render_and_empty(self):
        block = self.reg.render("B", "议价")
        self.assertIn("## 技能：b-bargain", block)
        self.assertIn("议价技能", block)
        self.assertEqual(self.reg.render("B", "不存在的场景", client_config={"skills_enabled": []}), "")

    def test_render_uses_client_config(self):
        block = self.reg.render("B", "议价", client_config={"skills_disabled": ["b-bargain"]})
        self.assertNotIn("b-bargain", block)
        self.assertIn("b-generic", block)


class TestShippedSkills(unittest.TestCase):
    """首批 11 个技能包的门禁：能加载、有来源、B 带红线、字数在预算内。"""

    @classmethod
    def setUpClass(cls):
        cls.reg = SkillRegistry.load(SKILLS_DIR)

    def test_count(self):
        agents = {"A": 0, "B": 0}
        for s in self.reg.all():
            agents[s.agent] += 1
        self.assertEqual(agents, {"A": 5, "B": 6})

    def test_every_skill_has_pinned_source(self):
        for s in self.reg.all():
            self.assertTrue(s.source.get("repo"), f"{s.name} 缺 source.repo")
            self.assertTrue(s.source.get("commit"), f"{s.name} 缺 source.commit（必须锁版本）")
            self.assertEqual(s.source.get("license"), "MIT", f"{s.name} 许可证不是 MIT")
            self.assertIs(s.source.get("adapted"), True, f"{s.name} 必须标 adapted: true（不能直接装原版）")

    def test_agent_b_skills_carry_red_lines(self):
        for s in self.reg.all():
            if s.agent != "B":
                continue
            self.assertIn("红线", s.body, f"{s.name} 缺红线段落")
            hits = [w for w in RED_LINE_WORDS if w in s.body]
            self.assertTrue(hits, f"{s.name} 红线段落没有点名任何红线项")

    def test_agent_a_skills_forbid_stock_numbers(self):
        for s in self.reg.all():
            if s.agent != "A":
                continue
            self.assertTrue("库存数" in s.body or "个人信息" in s.body, f"{s.name} 缺红线")

    def test_budget_fits_generic_plus_scenario(self):
        # B 在「议价」场景：通用 3 个 + 议价 2 个，必须能在默认预算内全部装下
        picked = self.reg.select("B", "议价")
        self.assertEqual(len(picked), 5, [s.name for s in picked])
        self.assertLessEqual(sum(s.chars for s in picked), 5000)

    def test_handoff_always_present_for_b(self):
        for scenario in ("首次询盘", "议价", "下单", "投诉", "闲聊"):
            names = [s.name for s in self.reg.select("B", scenario)]
            self.assertIn("yiwu-handoff-detection", names, scenario)
            self.assertEqual(names[0], "yiwu-handoff-detection", f"{scenario}: 转人工必须最先注入")


if __name__ == "__main__":
    unittest.main()
