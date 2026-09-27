"""引擎库条目物理删除 —— 磁盘 rm -rf + 注册表清理 + 残留源码引用报告。

spec `docs/superpowers/specs/2026-07-28-model-physical-delete-design.md`。

为什么独立成模块:`src/api/routes/engines.py` 已 950 行,而删除是自成一体的一块
(目标解析 / 路径安全 / 预检 / 执行 / 注册表清理 / 残留扫描),塞进路由文件只会继续
膨胀。路由那边只留两个薄端点。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.config import get_settings


class DeleteError(Exception):
    """带 HTTP 状态码的删除失败 —— 路由层直接转 HTTPException。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class Target:
    """一个引擎库条目对应的物理删除目标。"""

    name: str
    kind: str  # 恒为 model(2026-09-26 起超分/组件/LoRA 条目随自建图像引擎删除)
    path: Path
    is_dir: bool
    engine_key: str | None = None
    local_path: str | None = None


def _models_root() -> Path:
    return Path(get_settings().LOCAL_MODELS_PATH)


def allowed_roots() -> list[Path]:
    """可删范围的白名单根:只有 LOCAL_MODELS_PATH。

    2026-09-26 起 LoRA 条目随自建图像引擎删除,LORA_PATHS 不再进白名单
    (删除目标只剩 LOCAL_MODELS_PATH/<local_path> 整模型目录)。
    """
    roots = [_models_root()]
    out: list[Path] = []
    for r in roots:
        try:
            rr = r.resolve()
        except OSError:  # pragma: no cover — 根不可达时跳过,不让它拖垮整次删除
            continue
        if rr not in out:
            out.append(rr)
    return out


def assert_safe_target(target: Target) -> None:
    """删任何东西之前的硬闸门。不通过 → DeleteError(400)。原地修正 `is_dir`。

    容器检查刻意**只 resolve 父目录**,不 resolve 叶子:叶子若是软链,resolve 会指到
    根外的真实目标,既会误判越界、也会诱导顺着链接删到别处。父目录 resolve 已足够
    挡住路径中段的 `..` 与软链逃逸。
    """
    path = target.path
    try:
        real_parent = path.parent.resolve()
    except OSError as e:
        raise DeleteError(400, f"目标路径不可解析: {path} ({e})") from e
    candidate = real_parent / path.name

    roots = allowed_roots()
    root = next((r for r in roots if candidate == r or _is_under(candidate, r)), None)
    if root is None:
        raise DeleteError(
            400,
            f"目标 {candidate} 不在允许的模型根内(允许: {', '.join(str(r) for r in roots)})",
        )
    if candidate == root:
        raise DeleteError(400, f"拒绝删除模型根本身: {candidate}")

    # 目录必须在根下至少两层，避免删除 media/、llm/、speech/ 等类型目录。
    # 只查目录(文件永远不是类型目录;软链目录已在下方降级为只删链接)。
    rel_parts = candidate.relative_to(root).parts
    if target.is_dir and len(rel_parts) < 2:
        raise DeleteError(400, f"拒绝删除类型目录(深度不足): {candidate}")

    # 软链只删链接本身。放在最后,保证越界/深度判定先跑。
    if path.is_symlink():
        target.is_dir = False


def path_size_bytes(path: Path) -> int:
    """目标占盘字节数(删除前算,用于「将释放 X GB」)。软链按链接自身算,不跟进目标。"""
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        if not path.is_dir():
            return 0
        return sum(
            f.lstat().st_size
            for f in path.rglob("*")
            if not f.is_symlink() and f.is_file()
        )
    except OSError:
        return 0


def delete_disk(target: Target) -> tuple[int, list[str]]:
    """真删磁盘。返回 (释放字节数, 失败项说明列表)。

    **不抛异常**:删不动的项收进 errors 返回,让调用方仍能执行注册表清理,并把
    「磁盘只删掉一半」如实报给用户,而不是静默或半途中断。
    """
    import shutil  # noqa: PLC0415 — 只在真删路径用到

    size = path_size_bytes(target.path)
    errors: list[str] = []

    def _on_error(_func, path, exc_info):
        errors.append(f"{path}: {exc_info[1]}")

    try:
        if target.is_dir:
            shutil.rmtree(target.path, onerror=_on_error)
        else:
            os.unlink(target.path)
    except FileNotFoundError:
        return 0, []
    except OSError as e:
        errors.append(f"{target.path}: {e}")
        return 0, errors

    if errors:
        # 部分失败 → 用剩余占盘反算真正释放的量,别谎报整个 size。
        return max(0, size - path_size_bytes(target.path)), errors
    return size, errors


def models_d_dir() -> Path:
    """`backend/configs/models.d/` —— 模型静态定义的单一来源(一模型一文件)。"""
    from src.config import _resolve_path  # noqa: PLC0415

    return Path(_resolve_path("configs/models.d"))


def delete_models_d_yaml(engine_key: str, models_d: Path | None = None) -> bool:
    """删 `models.d/<engine_key>.yaml`。返回是否真的删了(自动发现的模型本就没有)。

    legacy `configs/models.yaml` 的 `models:` list 不做 YAML 结构改写(它当前是空锚点);
    真有条目命中时由残留引用报告交人工处理。
    """
    if "/" in engine_key or "\\" in engine_key or ".." in engine_key:
        raise DeleteError(400, f"非法 engine key: {engine_key}")
    d = Path(models_d) if models_d is not None else models_d_dir()
    f = d / f"{engine_key}.yaml"
    if not f.is_file():
        return False
    f.unlink()
    return True


async def count_registry_rows(session, engine_key: str) -> tuple[bool, int]:
    """预检用的只读版本:(有无 model_metadata 行, model_runtime_overrides 行数)。"""
    from sqlalchemy import select  # noqa: PLC0415

    from src.models.model_metadata import ModelMetadata  # noqa: PLC0415
    from src.models.model_runtime_override import ModelRuntimeOverride  # noqa: PLC0415

    has_meta = (
        await session.execute(
            select(ModelMetadata.id).where(ModelMetadata.engine_key == engine_key)
        )
    ).first() is not None
    n_ov = len(
        (
            await session.execute(
                select(ModelRuntimeOverride.model_id).where(
                    ModelRuntimeOverride.model_id == engine_key
                )
            )
        )
        .scalars()
        .all()
    )
    return has_meta, n_ov


async def clean_registry_db(session, engine_key: str) -> dict:
    """清 DB 侧注册表:model_metadata 行 + model_runtime_overrides 行。"""
    from sqlalchemy import delete, select  # noqa: PLC0415

    from src.models.model_metadata import ModelMetadata  # noqa: PLC0415
    from src.models.model_runtime_override import ModelRuntimeOverride  # noqa: PLC0415

    had_meta = (
        await session.execute(
            select(ModelMetadata.id).where(ModelMetadata.engine_key == engine_key)
        )
    ).first() is not None
    if had_meta:
        await session.execute(
            delete(ModelMetadata).where(ModelMetadata.engine_key == engine_key)
        )

    ov_ids = (
        (
            await session.execute(
                select(ModelRuntimeOverride.model_id).where(
                    ModelRuntimeOverride.model_id == engine_key
                )
            )
        )
        .scalars()
        .all()
    )
    if ov_ids:
        await session.execute(
            delete(ModelRuntimeOverride).where(
                ModelRuntimeOverride.model_id == engine_key
            )
        )

    await session.commit()
    return {"model_metadata": had_meta, "runtime_overrides": len(ov_ids)}


async def find_referencing_services(session, engine_key: str) -> list[dict]:
    """引用该模型的服务实例(source_type='model' 且 source_name=<key>)= 软 blocker。

    删模型不动服务本身,但用户有权在确认框里看到「这几个接入点会指向不存在的模型」。
    """
    from sqlalchemy import select  # noqa: PLC0415

    from src.models.service_instance import ServiceInstance  # noqa: PLC0415

    rows = (
        await session.execute(
            select(ServiceInstance.id, ServiceInstance.name).where(
                ServiceInstance.source_type == "model",
                ServiceInstance.source_name == engine_key,
            )
        )
    ).all()
    return [{"id": str(r[0]), "name": r[1]} for r in rows]


def invalidate_all_caches() -> None:
    """删完清 3 层缓存,否则引擎库最长 30s 内仍显示已删的条目。

    刻意按**模块属性**调用(而非 from-import 绑定):这些失效函数在测试里被 monkeypatch,
    早绑定会拿到打补丁前的旧引用。
    """
    from src.api import response_cache  # noqa: PLC0415
    from src.services import model_metadata_service  # noqa: PLC0415
    from src.services import model_scanner  # noqa: PLC0415

    model_scanner.invalidate_scan_cache()
    model_metadata_service.invalidate_local_scan_cache()
    response_cache.invalidate("engines")


_MIN_TERM_LEN = 4
_SCAN_SUFFIXES = {".py", ".ts", ".tsx", ".yaml", ".yml", ".md", ".sh", ".json"}
_SCAN_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "__pycache__"}
_SCAN_TIMEOUT_SECONDS = 20


def scan_code_refs(
    terms: list[str], repo_root: Path | None = None, limit: int = 200
) -> dict:
    """扫仓库里还引用被删模型的源文件。**只报不删**。

    优先 `git grep`(自动跳 .gitignore 覆盖的 .venv/node_modules/dist,也最快);
    非 git 或 git 不可用时回落到后缀白名单遍历。扫描失败绝不影响删除成败 —— 返回空
    列表 + `scan_error` 说明。
    """
    root = Path(repo_root) if repo_root else _repo_root()
    wanted = sorted({t for t in terms if t and len(t) >= _MIN_TERM_LEN})
    if not wanted:
        return {"refs": [], "truncated": False, "scan_error": None}

    refs: list[dict] = []
    error: str | None = None
    try:
        if (root / ".git").exists():
            refs = _git_grep(root, wanted)
        else:
            refs = _walk_grep(root, wanted)
    except Exception as e:  # noqa: BLE001 — 扫描是尽力而为,绝不拖垮删除
        error = f"{type(e).__name__}: {e}"
        refs = []

    truncated = len(refs) > limit
    return {"refs": refs[:limit], "truncated": truncated, "scan_error": error}


def drop_refs_to(refs: list[dict], path: str | None) -> list[dict]:
    """从残留引用里剔除指向 `path` 的条目。

    残留扫描跑在删除**之前**(避免为一次删除跑两遍 git grep),所以
    `models.d/<key>.yaml` 会把自己扫进去 —— 而它紧接着就被删了。不剔除的话报告会让
    用户去清一个已经不存在的文件。`refs` 里的 file 可能是仓库相对路径(git grep)也可能
    是绝对路径,统一解析后比对。
    """
    if not path:
        return refs
    target = Path(path).resolve()
    root = _repo_root()
    out: list[dict] = []
    for r in refs:
        f = Path(r.get("file", ""))
        full = f if f.is_absolute() else root / f
        try:
            if full.resolve() == target:
                continue
        except OSError:  # pragma: no cover — 解析不了就当它不是被删的那个
            pass
        out.append(r)
    return out


def _repo_root() -> Path:
    # src/services/model_deleter.py → backend/src/services → backend → 仓库根
    return Path(__file__).resolve().parents[3]


def _git_grep(root: Path, terms: list[str]) -> list[dict]:
    import subprocess  # noqa: PLC0415

    cmd = ["git", "grep", "-n", "-I", "-F"]
    for t in terms:
        cmd += ["-e", t]
    proc = subprocess.run(
        cmd, cwd=root, capture_output=True, text=True, timeout=_SCAN_TIMEOUT_SECONDS
    )
    # rc 0 = 有命中,1 = 无命中(都正常);其余 = git 出错 → 回落遍历。
    if proc.returncode not in (0, 1):
        return _walk_grep(root, terms)
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        file, sep, rest = line.partition(":")
        if not sep:
            continue
        lineno, sep2, text = rest.partition(":")
        if not sep2 or not lineno.isdigit():
            continue
        out.append({"file": file, "line": int(lineno), "text": text.strip()[:300]})
    return out


def _walk_grep(root: Path, terms: list[str]) -> list[dict]:
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix not in _SCAN_SUFFIXES:
                continue
            try:
                content = p.read_text(errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(content.splitlines(), start=1):
                if any(t in line for t in terms):
                    out.append(
                        {
                            "file": str(p.relative_to(root)),
                            "line": i,
                            "text": line.strip()[:300],
                        }
                    )
    return out


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_target(name: str, configs: dict) -> Target:
    """引擎库条目 name → 物理删除目标。

    `configs` = `scan_models()` 结果(registry + 自动发现的整模型)。
    未知 name → 404;name 含 `..` → 400(在碰磁盘之前就拒)。
    """
    if ".." in Path(name).parts or "/.." in name or name.startswith(".."):
        raise DeleteError(400, f"非法条目名(含 .. 路径段): {name}")

    cfg = configs.get(name)
    if cfg is None:
        raise DeleteError(404, f"未知引擎条目: {name}")
    local_path = cfg.get("local_path")
    if not local_path:
        raise DeleteError(400, f"{name} 无 local_path,无法定位磁盘目标")
    return Target(
        name=name,
        kind="model",
        path=_models_root() / local_path,
        is_dir=True,
        engine_key=name,
        local_path=local_path,
    )
