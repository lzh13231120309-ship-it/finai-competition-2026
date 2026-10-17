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
            if name in {"产业链影响分析", "投资备忘录编制"}:
                continue  # Retired standalone directions; keep five competition capabilities.
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


def _seed_state_path():
    return config.DATA_DIR / 'financial-skill-versions.json'


def _load_seed_state() -> dict:
    import json
    try:
        return json.loads(_seed_state_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save_seed_state(state: dict) -> None:
    import json
    try:
        _seed_state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                     encoding='utf-8')
    except OSError:
        pass


def _sha256_file(path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def seed_financial_skills() -> None:
    """把随包发布的内置技能与知识库同步到数据目录，但不覆盖主人改过的内容。

    为什么要"带上全部内置技能"而不是写死两个名字：
    早先只同步 financial-structured-extraction 与 listed-company-financial-analysis，
    后来新增的 forensic-anomaly-screening / valuation-stress-testing 因此
    **从来没有进到 .app 里** —— 开发时一切正常，打包后技能列表里就是没有它，
    属于最难察觉的一类问题。现在按目录扫描，新增技能自动跟随。

    守卫：只有当目标文件不存在，或内容与"上次同步时的哈希"一致（意味着主人没改过）
    才覆盖。主人手工编辑过的技能永远保留。
    """
    import shutil
    roots = [config.ROOT / 'seed' / 'skills', config.ROOT / 'data' / 'skills']
    state = _load_seed_state()

    seen: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not (child / 'SKILL.md').exists():
                continue
            name = child.name
            if name in seen:
                continue          # 前一个根目录里的同名技能优先，后者不覆盖
            dest = config.SKILLS_DIR / name
            if dest.resolve() == child.resolve():
                seen.append(name)
                continue          # 开发模式：数据目录就是源目录，无需复制
            key = f'skill:{name}'
            old_hash = _sha256_file(dest / 'SKILL.md')
            # state 里旧版本用的是裸技能名（无 skill: 前缀），一并认作"未改动"，
            # 否则历史已同步的技能会被判成"主人改过"而从此不再更新。
            untouched = old_hash is None or old_hash in (state.get(key), state.get(name))
            if untouched:
                try:
                    shutil.copytree(child, dest, dirs_exist_ok=True)
                except OSError:
                    continue
                new_hash = _sha256_file(dest / 'SKILL.md')
                if new_hash:
                    state[key] = new_hash
            seen.append(name)

    _save_seed_state(state)


def seed_knowledge_base() -> None:
    """把随包发布的本地知识库同步到数据目录，并为新库及本次成功同步内容变化的库建索引。

    与技能同样的守卫：主人改过的文件不覆盖。
    建索引限定在"尚未建过索引的库"，避免每次启动都重算，也避免把主人在界面上
    新建的知识库顺手重建。
    """
    import shutil
    from . import knowledge
    roots = [config.ROOT / 'seed' / 'knowledge', config.ROOT / 'data' / 'knowledge']
    state = _load_seed_state()

    seen: set[str] = set()
    changed: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith('.'):
                continue
            if child.name in seen:
                continue
            seen.add(child.name)
            dest = config.KNOWLEDGE_DIR / child.name
            if dest.resolve() == child.resolve():
                continue          # 开发模式：同一目录
            for src_file in sorted(child.rglob('*')):
                if not src_file.is_file() or src_file.name.startswith('.'):
                    continue
                rel = src_file.relative_to(child)
                dst_file = dest / rel
                key = f'kb:{child.name}/{rel}'
                old_hash = _sha256_file(dst_file)
                if old_hash is not None and old_hash != state.get(key):
                    continue      # 主人改过这一份，保留
                new_hash = _sha256_file(src_file)
                if old_hash == new_hash:
                    state[key] = new_hash
                    continue
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src_file, dst_file)
                    changed.add(child.name)
                except OSError:
                    continue
                if new_hash:
                    state[key] = new_hash

    _save_seed_state(state)

    # 只为尚未建立索引的知识库建索引（跳过主人已建过索引的）
    try:
        from . import db, knowledge
        stats = db.kb_stats()
        for kb_info in knowledge.list_kbs():
            name = kb_info['name']
            if name in changed or not (stats.get(name, {}) or {}).get('chunks'):
                knowledge.index_kb(name)
    except Exception:
        pass

