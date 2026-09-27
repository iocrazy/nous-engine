"""File-based skill CRUD manager.

Skills are stored under ``~/.nous-center/skills/``.  Each skill is a directory
containing a ``SKILL.md`` file with YAML frontmatter.

SKILL.md format::

    ---
    name: tts-synthesis
    description: 将文本合成为语音
    requires:
      models: ["cosyvoice2"]
    ---

    ## 使用说明
    ...
"""

import shutil
from pathlib import Path

from src.config import get_settings
from src.utils.path_security import validate_path as _validate_path_raw


def _skills_dir() -> Path:
    home = Path(get_settings().NOUS_CENTER_HOME).expanduser()
    return home / "skills"


def _validate_path(base: Path, untrusted: str) -> Path:
    return _validate_path_raw(base / untrusted, base)


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split SKILL.md into (frontmatter_dict, body_text)."""
    from src.utils.frontmatter import split_frontmatter

    return split_frontmatter(raw)


def _build_raw(name: str, description: str, body: str) -> str:
    """Build SKILL.md raw content from parts."""
    import yaml

    fm = {"name": name, "description": description}
    fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{fm_str}\n---\n\n{body}"


def list_skills() -> list[dict]:
    """Return list of skills with name, description, requires."""
    base = _skills_dir()
    if not base.exists():
        return []
    skills = []
    for d in sorted(base.iterdir()):
        md = d / "SKILL.md"
        if d.is_dir() and md.exists():
            fm, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
            skills.append({
                "name": fm.get("name", d.name),
                "description": fm.get("description", ""),
                "requires": fm.get("requires", {}),
            })
    return skills


def get_skill(name: str) -> dict:
    """Return full skill content: frontmatter fields + body + raw."""
    skill_dir = _validate_path(_skills_dir(), name)
    md = skill_dir / "SKILL.md"
    if not md.exists():
        raise FileNotFoundError(f"Skill '{name}' not found")
    raw = md.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(raw)
    return {
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "requires": fm.get("requires", {}),
        "body": body,
        "raw": raw,
    }


def create_skill(name: str, description: str = "", body: str = "") -> dict:
    """Create a new skill directory with SKILL.md."""
    skill_dir = _validate_path(_skills_dir(), name)
    if skill_dir.exists():
        raise FileExistsError(f"Skill '{name}' already exists")
    skill_dir.mkdir(parents=True)
    raw = _build_raw(name, description, body)
    (skill_dir / "SKILL.md").write_text(raw, encoding="utf-8")
    return {"name": name, "description": description, "requires": {}}


def update_skill(name: str, raw_content: str) -> dict:
    """Overwrite SKILL.md with raw content."""
    skill_dir = _validate_path(_skills_dir(), name)
    md = skill_dir / "SKILL.md"
    if not md.exists():
        raise FileNotFoundError(f"Skill '{name}' not found")
    md.write_text(raw_content, encoding="utf-8")
    fm, body = _parse_frontmatter(raw_content)
    return {
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "requires": fm.get("requires", {}),
        "body": body,
        "raw": raw_content,
    }


def delete_skill(name: str) -> None:
    """Remove skill directory."""
    skill_dir = _validate_path(_skills_dir(), name)
    if not skill_dir.exists():
        raise FileNotFoundError(f"Skill '{name}' not found")
    shutil.rmtree(skill_dir)
