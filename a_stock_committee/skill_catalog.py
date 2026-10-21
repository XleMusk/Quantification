"""Local skill catalog discovery for the A-share committee."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


SKILLS_DIR = Path(__file__).parent / "skills"


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    body_preview: str


def list_skills(skills_dir: Path = SKILLS_DIR) -> list[SkillInfo]:
    """Discover local skills by reading each SKILL.md frontmatter."""
    items: list[SkillInfo] = []
    if not skills_dir.exists():
        return items

    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text)
        items.append(
            SkillInfo(
                name=meta.get("name") or skill_md.parent.name,
                description=meta.get("description") or "",
                path=skill_md.parent,
                body_preview=_preview(body),
            )
        )
    return items


def get_skill(name: str, skills_dir: Path = SKILLS_DIR) -> SkillInfo:
    """Find one skill by folder name or frontmatter name."""
    normalized = name.strip().lower()
    for item in list_skills(skills_dir):
        if normalized in {item.name.lower(), item.path.name.lower()}:
            return item
    raise KeyError(f"Skill not found: {name}")


def skill_detail_markdown(name: str, skills_dir: Path = SKILLS_DIR) -> str:
    """Return a readable skill detail document."""
    item = get_skill(name, skills_dir)
    skill_md = item.path / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    _, body = _split_frontmatter(text)
    refs = sorted((item.path / "references").glob("*.md"))
    scripts = sorted((item.path / "scripts").glob("*.py"))

    lines = [
        f"# {item.name}",
        "",
        item.description,
        "",
        f"- 路径：{item.path}",
    ]
    if refs:
        lines.append("- 参考文件：")
        lines.extend([f"  - {ref.name}" for ref in refs])
    if scripts:
        lines.append("- 脚本：")
        lines.extend([f"  - {script.name}" for script in scripts])
    lines.extend(["", body.strip(), ""])
    return "\n".join(lines)


def catalog_markdown(skills_dir: Path = SKILLS_DIR) -> str:
    """Return the full catalog as Markdown."""
    lines = ["# A股 Skills 目录", ""]
    for item in list_skills(skills_dir):
        lines.extend(
            [
                f"## {item.name}",
                "",
                item.description,
                "",
                f"- 路径：{item.path}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, parts[2]


def _preview(body: str, limit: int = 260) -> str:
    compact = " ".join(line.strip() for line in body.splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."

