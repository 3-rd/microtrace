"""Prompts loader (SPEC.md §5)"""
from __future__ import annotations
from pathlib import Path
from functools import lru_cache

# agent.md 在项目根 prompts/ 下，不在 src/microtrace/prompts/
# src/microtrace/prompts/__init__.py → ../../.. → 项目根
_PACKAGE_DIR = Path(__file__).parent
_PROJECT_ROOT = _PACKAGE_DIR.parent.parent.parent
PROMPTS_DIR = _PROJECT_ROOT / "prompts"
AGENT_PROMPT_FILE = PROMPTS_DIR / "agent.md"


@lru_cache(maxsize=1)
def load_agent_prompt() -> str:
    """加载 master prompt（agent.md 全文）"""
    if not AGENT_PROMPT_FILE.exists():
        return ""
    return AGENT_PROMPT_FILE.read_text(encoding="utf-8")


def get_section(name: str) -> str:
    """从 agent.md 提取指定 ## section（按 ## 分割，name 不含 ##）"""
    content = load_agent_prompt()
    if not content:
        return ""
    lines = content.split("\n")
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            section_name = line[3:].strip()
            if section_name == name or section_name.split(" ", 1)[-1] == name:
                in_section = True
                continue
        elif line.startswith("# "):
            if in_section:
                break
        if in_section:
            section_lines.append(line)
    return "\n".join(section_lines).strip()


def clear_cache() -> None:
    """清掉 lru_cache（测试用）"""
    load_agent_prompt.cache_clear()
