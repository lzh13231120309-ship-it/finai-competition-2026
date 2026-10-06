"""Skills 管理：扫描 SKILL.md、按需注入系统提示、支持导入导出。

技能目录结构（与主流 Agent 工具一致）：
    data/skills/<技能名>/SKILL.md
`SKILL.md` 支持 YAML frontmatter：name / description 等字段。
技能采用「渐进式披露」：系统提示里只放名称与描述，模型判断需要时才通过
`load_skill` 工具读取全文，避免上下文被塞满。
"""

import re
import shutil
from pathlib import Path

from . import config

MAX_SKILL_TEXT = 40000


def skill_dirs() -> list[Path]:
    cfg = config.load()
    dirs = [config.SKILLS_DIR]
    for p in cfg.get("extra_skill_dirs", []) or []:
        d = Path(p).expanduser()
        if d.exists():
            dirs.append(d)
    return dirs


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    meta: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            raw, body = parts[1], parts[2]
            for line in raw.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body.strip()


def _first_paragraph(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:120]
    for line in body.splitlines():
        if line.strip().startswith("#"):
            return line.strip("# ").strip()[:120]
    return ""


def list_skills() -> list[dict]:
    cfg = config.load()
    enabled_map = cfg.get("skills_enabled", {}) or {}
    seen: dict[str, dict] = {}
    for root in skill_dirs():
        if not root.exists():
            continue
        # 支持两种布局：<root>/<技能名>/SKILL.md 与 &lt;root&gt;/SKILL.md
        candidates = []
        if (root / "SKILL.md").exists():
            candidates.append(root)
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "SKILL.md").exists():
                candidates.append(child)
        for d in candidates:
            try:
                raw = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name") or d.name
            if name in seen:
                continue
            seen[name] = {
                "name": name,
                "description": meta.get("description") or _first_paragraph(body),
                "path": str(d),
                "source": "内置" if root == config.SKILLS_DIR else str(root),
                "size": len(raw),
                "enabled": enabled_map.get(name, True),
                "agent_created": str(meta.get("agent_created", "")).lower() == "true",
            }
    return sorted(seen.values(), key=lambda x: x["name"])


def get_skill(name: str) -> dict | None:
    for s in list_skills():
        if s["name"] == name:
            return s
    return None


def read_skill_text(name: str) -> str:
    s = get_skill(name)
    if not s:
        return ""
    raw = (Path(s["path"]) / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    _, body = _parse_frontmatter(raw)
    if len(body) > MAX_SKILL_TEXT:
        body = body[:MAX_SKILL_TEXT] + "\n…（技能内容过长已截断）"
    return body


def catalog_for_prompt() -> str:
    skills = [s for s in list_skills() if s["enabled"]]
    if not skills:
        return ""
    lines = ["## 可用技能（Skills）",
             "当任务与下列技能高度相关时，先调用 load_skill 工具读取该技能全文，再按其中的步骤执行。"]
    for s in skills:
        lines.append(f"- `{s['name']}`：{s['description']}")
    return "\n".join(lines)


def set_enabled(name: str, enabled: bool):
    cfg = config.load()
    m = dict(cfg.get("skills_enabled", {}) or {})
    m[name] = bool(enabled)
    config.update({"skills_enabled": m})


def import_skill(source: str) -> dict:
    src = Path(source).expanduser()
    if not src.exists():
        return {"ok": False, "error": "路径不存在"}
    if src.is_file() and src.name.lower() == "skill.md":
        src = src.parent
    if not (src / "SKILL.md").exists() and not (src.name.lower() == "skill.md"):
        return {"ok": False, "error": "该目录下没有找到 SKILL.md"}
    dest = config.SKILLS_DIR / src.name
    if dest.exists() and dest.resolve() != src.resolve():
        shutil.rmtree(dest)
    if dest.resolve() != src.resolve():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    return {"ok": True, "name": src.name, "path": str(dest)}


def create_skill(name: str, description: str, content: str) -> dict:
    safe = re.sub(r"[^\w\u4e00-\u9fff\-]+", "-", name).strip("-") or "new-skill"
    dest = config.SKILLS_DIR / safe
    dest.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {safe}\ndescription: {description}\nagent_created: true\n---\n\n{content}\n"
    (dest / "SKILL.md").write_text(text, encoding="utf-8")
    return {"ok": True, "name": safe}


def delete_skill(name: str) -> dict:
    s = get_skill(name)
    if not s:
        return {"ok": False, "error": "技能不存在"}
    p = Path(s["path"])
    if p.resolve() == config.SKILLS_DIR.resolve():
        return {"ok": False, "error": "不能删除技能根目录"}
    try:
        shutil.rmtree(p)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}
