# -*- coding: utf-8 -*-
"""
skill_loader.py — 店小力技能包加载器

把 skills/<agent>/*.md（SKILL.md 格式）按「Agent + 场景」挑出来，拼成一段可直接
塞进 system prompt 的文本。零第三方依赖；解析、排序、预算全部确定性，方便门禁回放。

用法（代码内）：
    from skill_loader import SkillRegistry
    reg = SkillRegistry.load("skills")
    block = reg.render(agent="B", scenario="议价", client_id="lifong")
    system_prompt = BASE_PROMPT + "\n\n" + block

用法（命令行预览）：
    python skill_loader.py --agent B --scenario 议价
    python skill_loader.py --list

SKILL.md 头部（frontmatter）字段：
    name:        技能唯一名（必填，英文短横线）
    description: 一句话说明（必填）
    agent:       A | B                （必填）
    scenarios:   [议价, 异议, 通用]     （必填；含「通用」则该 Agent 任何场景都注入）
    priority:    整数，越大越先注入（默认 50）
    max_chars:   本技能正文上限（默认 1800，超出即视为写坏了，加载时报错）
    source:      来源信息（repo / commit / license / adapted）
    enabled:     true|false（默认 true；总开关）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

GENERIC = "通用"
VALID_AGENTS = {"A", "B"}
DEFAULT_PRIORITY = 50
DEFAULT_MAX_CHARS = 1800
DEFAULT_BUDGET_CHARS = 5000  # 一次注入的总预算（中文按字符估算，1 字 ≈ 1.3–1.6 token）

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


class SkillError(ValueError):
    pass


# ---------------------------------------------------------------- frontmatter
def _parse_scalar(v: str):
    v = v.strip()
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_strip_quotes(x.strip()) for x in inner.split(",")]
    return _strip_quotes(v)


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def parse_frontmatter(text: str):
    """极简 YAML 子集：key: value / key: [a, b] / 缩进一层的映射。返回 (meta, body)。"""
    m = _FM_RE.match(text)
    if not m:
        raise SkillError("缺少 frontmatter（--- ... ---）")
    raw, body = m.group(1), m.group(2)
    meta: Dict[str, object] = {}
    current_map: Optional[Dict[str, object]] = None
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indented = line.startswith("  ") or line.startswith("\t")
        key, sep, val = line.strip().partition(":")
        if not sep:
            raise SkillError(f"frontmatter 行无法解析：{line!r}")
        key = key.strip()
        if indented and current_map is not None:
            current_map[key] = _parse_scalar(val)
            continue
        if val.strip() == "":
            current_map = {}
            meta[key] = current_map
        else:
            current_map = None
            meta[key] = _parse_scalar(val)
    return meta, body.strip()


# ---------------------------------------------------------------- model
@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    agent: str
    scenarios: tuple
    priority: int
    max_chars: int
    enabled: bool
    body: str
    path: str
    source: dict = field(default_factory=dict)

    def matches(self, scenario: str) -> bool:
        return GENERIC in self.scenarios or scenario in self.scenarios

    @property
    def chars(self) -> int:
        return len(self.body)


def load_skill_file(path: str) -> Skill:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    meta, body = parse_frontmatter(text)
    for k in ("name", "description", "agent", "scenarios"):
        if k not in meta:
            raise SkillError(f"{path}: 缺少字段 {k}")
    agent = str(meta["agent"]).strip().upper()
    if agent not in VALID_AGENTS:
        raise SkillError(f"{path}: agent 必须是 A 或 B，得到 {agent!r}")
    scenarios = meta["scenarios"]
    if isinstance(scenarios, str):
        scenarios = [scenarios]
    if not scenarios:
        raise SkillError(f"{path}: scenarios 不能为空")
    name = str(meta["name"]).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", name):
        raise SkillError(f"{path}: name 只能用小写字母数字和短横线，得到 {name!r}")
    max_chars = int(meta.get("max_chars", DEFAULT_MAX_CHARS))
    if len(body) > max_chars:
        raise SkillError(f"{path}: 正文 {len(body)} 字超过上限 {max_chars}，技能写太长会撑爆提示词")
    if not body:
        raise SkillError(f"{path}: 正文为空")
    return Skill(
        name=name,
        description=str(meta["description"]).strip(),
        agent=agent,
        scenarios=tuple(str(s).strip() for s in scenarios),
        priority=int(meta.get("priority", DEFAULT_PRIORITY)),
        max_chars=max_chars,
        enabled=bool(meta.get("enabled", True)),
        body=body,
        path=path,
        source=dict(meta.get("source", {}) or {}),
    )


# ---------------------------------------------------------------- registry
class SkillRegistry:
    def __init__(self, skills: Iterable[Skill]):
        self._skills: Dict[str, Skill] = {}
        for s in skills:
            if s.name in self._skills:
                raise SkillError(f"技能重名：{s.name}（{s.path} 与 {self._skills[s.name].path}）")
            self._skills[s.name] = s

    @classmethod
    def load(cls, root: str) -> "SkillRegistry":
        found: List[Skill] = []
        for dirpath, _dirs, files in os.walk(root):
            for fn in sorted(files):
                if fn.lower().endswith(".md") and fn.upper() != "README.MD":
                    found.append(load_skill_file(os.path.join(dirpath, fn)))
        return cls(found)

    def __len__(self) -> int:
        return len(self._skills)

    def all(self) -> List[Skill]:
        return sorted(self._skills.values(), key=lambda s: (s.agent, -s.priority, s.name))

    def get(self, name: str) -> Skill:
        return self._skills[name]

    def select(
        self,
        agent: str,
        scenario: str,
        client_enabled: Optional[Set[str]] = None,
        client_disabled: Optional[Set[str]] = None,
        budget_chars: int = DEFAULT_BUDGET_CHARS,
    ) -> List[Skill]:
        """
        选技能：先按 Agent + 场景过滤，再按客户开关，再按优先级排序，最后按字符预算截断。
        排序确定性：priority 降序，同分按 name 升序——回放时永远得到同一个结果。
        """
        agent = agent.upper()
        picked = [
            s for s in self._skills.values()
            if s.enabled and s.agent == agent and s.matches(scenario)
        ]
        if client_enabled is not None:
            picked = [s for s in picked if s.name in client_enabled]
        if client_disabled:
            picked = [s for s in picked if s.name not in client_disabled]
        picked.sort(key=lambda s: (-s.priority, s.name))
        out, used = [], 0
        for s in picked:
            if used + s.chars > budget_chars:
                continue  # 装不下的跳过，继续看更小的（不打断，保证高优先级优先）
            out.append(s)
            used += s.chars
        return out

    def render(
        self,
        agent: str,
        scenario: str,
        client_id: Optional[str] = None,
        client_config: Optional[dict] = None,
        budget_chars: int = DEFAULT_BUDGET_CHARS,
    ) -> str:
        """
        client_config 形如 {"skills_enabled": [...], "skills_disabled": [...]}，
        通常来自该客户的 config.json。没有就用全部默认技能。
        """
        cfg = client_config or {}
        en = set(cfg["skills_enabled"]) if cfg.get("skills_enabled") is not None else None
        dis = set(cfg.get("skills_disabled") or [])
        chosen = self.select(agent, scenario, en, dis, budget_chars)
        if not chosen:
            return ""
        parts = [f"# 当前场景：{scenario}（已装技能 {len(chosen)} 个）"]
        for s in chosen:
            parts.append(f"\n## 技能：{s.name}\n{s.body}")
        return "\n".join(parts).strip()

    def manifest(self) -> List[dict]:
        """给 /status 或门禁报告用：装了什么、来自哪、哪个 commit。"""
        return [
            {
                "name": s.name, "agent": s.agent, "scenarios": list(s.scenarios),
                "priority": s.priority, "chars": s.chars, "enabled": s.enabled,
                "source": s.source,
            }
            for s in self.all()
        ]


# ---------------------------------------------------------------- CLI
def _main(argv=None):
    ap = argparse.ArgumentParser(description="店小力技能包加载器")
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills"))
    ap.add_argument("--agent", choices=sorted(VALID_AGENTS))
    ap.add_argument("--scenario", default=GENERIC)
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET_CHARS)
    ap.add_argument("--list", action="store_true", help="列出全部技能与来源")
    a = ap.parse_args(argv)
    reg = SkillRegistry.load(a.root)
    if a.list or not a.agent:
        for m in reg.manifest():
            src = m["source"].get("repo", "自研")
            print(f"[{m['agent']}] {m['name']:<32} p={m['priority']:<3} {m['chars']:>5}字  场景={','.join(m['scenarios'])}  来源={src}")
        print(f"\n共 {len(reg)} 个技能")
        return 0
    block = reg.render(a.agent, a.scenario, budget_chars=a.budget)
    print(block or "（该场景没有可注入的技能）")
    print(f"\n--- 共 {len(block)} 字 ---", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
