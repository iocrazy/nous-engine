import logging
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# All paths resolve relative to the backend/ directory (parent of src/)
_BACKEND_DIR = Path(__file__).resolve().parent.parent
SETTINGS_YAML_PATH = _BACKEND_DIR / "settings.yaml"


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379/0"
    DATABASE_URL: str = "postgresql+asyncpg://mindcenter:mindcenter@localhost:5432/mindcenter"

    # 路径收口(spec 2026-06-19):.env 只给两个绝对根 —— 模型库 + 代码库。各子根
    # 由 configs/model_roots.yaml 的相对结构派生(见 _derive_paths)。重格/搬盘只改这两行。
    MODELS_ROOT: str = "/media/heygo/program/models"
    REPOS_ROOT: str = "/media/heygo/program/projects-code/github-repos"

    # 以下 6 个**留空即从根派生**(_derive_paths 填);仍是真字段 —— 消费代码 settings.XXX
    # 不变、测试可 setattr 注入、需要时也能在 .env/settings.yaml 显式覆盖单个(可选,非必需)。
    LOCAL_MODELS_PATH: str = ""
    NAS_MODELS_PATH: str = ""
    NAS_OUTPUTS_PATH: str = ""
    LORA_PATHS: str = ""  # lora_scanner 扫描目录(可逗号分隔多个)
    COSYVOICE_REPO_PATH: str = ""
    INDEXTTS_REPO_PATH: str = ""

    @model_validator(mode="after")
    def _derive_paths(self) -> "Settings":
        """空值的子根从 MODELS_ROOT/REPOS_ROOT + model_roots.yaml 派生。已显式给的保留。"""
        r = _model_roots()
        m, repo = Path(self.MODELS_ROOT), Path(self.REPOS_ROOT)
        if not self.LOCAL_MODELS_PATH:
            self.LOCAL_MODELS_PATH = str(m / r["models"]["local"])
        if not self.NAS_MODELS_PATH:
            self.NAS_MODELS_PATH = self.LOCAL_MODELS_PATH  # NAS 已并进本地根
        if not self.NAS_OUTPUTS_PATH:
            self.NAS_OUTPUTS_PATH = str(m / r["models"]["outputs"])
        if not self.LORA_PATHS:
            self.LORA_PATHS = str(m / r["models"]["loras"])
        if not self.COSYVOICE_REPO_PATH:
            self.COSYVOICE_REPO_PATH = str(repo / r["repos"]["cosyvoice"])
        if not self.INDEXTTS_REPO_PATH:
            self.INDEXTTS_REPO_PATH = str(repo / r["repos"]["indextts"])
        return self

    VLLM_BASE_URL: str = "http://localhost:8100"
    VL_MODEL: str = "Qwen2.5-VL-7B-Instruct"  # default model for /api/v1/understand

    CACHE_TTL_SECONDS: int = 3600  # TTS cache TTL (1 hour)
    # 图像/产物签名 URL 有效期(秒)。服务层 API spec PR-4:输出交付 TTL 归服务层配置,
    # 不再是每个出图节点的 widget(用户:URL 有效期不该是节点的事,该是工作流 API 的功能)。
    IMAGE_URL_TTL_SECONDS: int = 3600

    ADMIN_TOKEN: str = ""  # Set to require auth for management API (CLI/curl bearer token)
    # Browser admin login: when ADMIN_PASSWORD is set, /api/* and /ws/* require
    # a valid session cookie obtained via POST /sys/admin/login. Empty disables
    # the gate (dev mode). ADMIN_SESSION_SECRET signs the cookie HMAC.
    ADMIN_PASSWORD: str = ""
    ADMIN_SESSION_SECRET: str = ""
    ADMIN_SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 30  # 30 days
    # WebAuthn / Passkey settings.
    # ADMIN_PASSKEY_RP_ID is the host the browser sends — must EXACTLY match
    # the domain the page is loaded from (no scheme, no port). Examples:
    #   prod (ZeroTier):  10.0.0.10   (公网隧道已退役,后端只在本机/内网可达)
    #   localhost dev:    localhost   (works without https for localhost only)
    # Multiple origins (dev + prod) are supported via a comma-separated list
    # in ADMIN_PASSKEY_RP_ORIGINS — every value must be `scheme://host[:port]`.
    ADMIN_PASSKEY_RP_ID: str = "localhost"
    ADMIN_PASSKEY_RP_NAME: str = "nous-center"
    ADMIN_PASSKEY_RP_ORIGINS: str = "http://localhost:9999,http://localhost:8000"

    NOUS_CENTER_HOME: str = "~/.nous-center"

    NOUS_ENABLE_AGENT_INJECTION: bool = False  # feature flag for agent/skill system prompt injection

    model_config = {"env_file": ".env", "extra": "ignore"}


def _resolve_path(relative: str) -> Path:
    """Resolve a path relative to the backend/ directory."""
    return _BACKEND_DIR / relative


# 路径收口(spec 2026-06-19):MODELS_ROOT/REPOS_ROOT 下的相对子根。
# fail-soft —— 文件缺失/坏时退回内置默认(与 model_roots.yaml 同值),绝不让缺配置崩启动。
MODEL_ROOTS_YAML_PATH = _BACKEND_DIR / "configs" / "model_roots.yaml"
_MODEL_ROOTS_DEFAULT = {
    "models": {"local": "nous", "loras": "nous/media/loras", "outputs": "nous/outputs"},
    "repos": {"cosyvoice": "CosyVoice", "indextts": "index-tts"},
}


@lru_cache
def _model_roots() -> dict:
    if not MODEL_ROOTS_YAML_PATH.exists():
        return _MODEL_ROOTS_DEFAULT
    try:
        with open(MODEL_ROOTS_YAML_PATH) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return _MODEL_ROOTS_DEFAULT
        # 浅合并到默认 —— 缺某段/某键时仍有兜底,不抛 KeyError。
        merged = {k: dict(v) for k, v in _MODEL_ROOTS_DEFAULT.items()}
        for sect in ("models", "repos"):
            if isinstance(data.get(sect), dict):
                merged[sect].update(data[sect])
        return merged
    except Exception:
        return _MODEL_ROOTS_DEFAULT


@lru_cache
def get_settings() -> Settings:
    overrides = _load_settings_yaml()
    return Settings(**overrides)


def _load_settings_yaml() -> dict:
    """Load overrides from settings.yaml if it exists."""
    if not SETTINGS_YAML_PATH.exists():
        return {}
    try:
        with open(SETTINGS_YAML_PATH) as f:
            data = yaml.safe_load(f) or {}
        # round4 #5(config):手改成 YAML 列表/标量时 safe_load 返回非 dict(truthy 不被
        # `or {}` 兜),get_settings 的 `Settings(**data)` 会 TypeError 崩。对齐
        # load_hardware_config 的 isinstance 守卫,非 mapping 降级空 dict。
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def save_settings(updates: dict) -> None:
    """Merge updates into settings.yaml and clear the cached Settings."""
    existing = _load_settings_yaml()
    existing.update(updates)

    with open(SETTINGS_YAML_PATH, "w") as f:
        yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)

    get_settings.cache_clear()


# 运行时/每机覆盖(resident 常驻、gpu 指派、vram_budget 显存预算)。
# 数据加载统一(2026-06-16,用户拍「拆数据表」):从 gitignore 的 runtime_overrides.json
# 文件迁到 Postgres typed 表 model_runtime_overrides(与服务/key 同库),拆成正经列。
# 读取仍同步(走 runtime_override_store 的进程内缓存,启动时从 DB hydrate);写在异步
# API handler 里走 runtime_override_store.set_override。**叠加在 models.yaml 之上**(overlay 优先)。
# 旧 JSON 路径仅留作一次性迁移源(migrate_json_if_empty)。
_RUNTIME_OVERRIDES_REL = "configs/runtime_overrides.json"
# vram_budget:每模型显存预算({"mode":"auto|percent|absolute","value":N})。
_OVERRIDABLE_KEYS = ("resident", "gpu", "gpus", "vram_budget")


def load_runtime_overrides() -> dict:
    """运行时覆盖快照(同步)。真相源 = DB(runtime_override_store 缓存,启动 hydrate)。
    局部 import 断 config↔store↔database↔config 循环。未 hydrate(早期/无 DB 的测试)→ 空 dict。"""
    from src.services import runtime_override_store  # noqa: PLC0415
    return runtime_override_store.get_overrides()


def _entries_from_doc(doc: object) -> list[dict]:
    """一个 models.d 文件:既支持顶层即单个模型 dict,也支持 {models:[...]} 容错。"""
    if not isinstance(doc, dict):
        return []
    if isinstance(doc.get("models"), list):
        return [e for e in doc["models"] if isinstance(e, dict)]
    if doc.get("id"):
        return [doc]
    return []


def collect_model_entries(yaml_path: Path) -> list[dict]:
    """模型静态定义的**单一来源**:合并 `<dir>/models.d/*.yaml`(一模型一文件)+ yaml_path
    自身的 `models:` list。按 id 去重(models.d 优先)。load_model_configs 与 ModelRegistry
    两个读取器都走这个,避免口径分叉(spec 2026-06-20)。
    """
    yaml_path = Path(yaml_path)  # _resolve_path 可能返回 str(测试 monkeypatch);统一成 Path
    entries: list[dict] = []
    seen: set[str] = set()

    def _add(e: dict) -> None:
        mid = e.get("id")
        if mid and mid not in seen:
            entries.append(e)
            seen.add(mid)

    models_d = yaml_path.parent / "models.d"
    if models_d.is_dir():
        for f in sorted(models_d.glob("*.yaml")):
            try:
                doc = yaml.safe_load(f.read_text())
            except Exception:  # noqa: BLE001 — 单个坏文件不阻断其余
                logger.warning("models.d 文件解析失败,跳过: %s", f.name)
                continue
            for e in _entries_from_doc(doc):
                _add(e)

    if yaml_path.exists():
        try:
            data = yaml.safe_load(yaml_path.read_text()) or {}
        except Exception:  # noqa: BLE001
            data = {}
        legacy = data.get("models") if isinstance(data, dict) else None
        if isinstance(legacy, list):
            for e in legacy:
                if isinstance(e, dict):
                    _add(e)
    return entries


def load_model_configs(path: str = "configs/models.yaml",
                      apply_overrides: bool = True) -> dict:
    """Load model configs and return dict keyed by model id/name.

    模型定义走 collect_model_entries(models.d/*.yaml + models.yaml,单一来源)。
    运行时覆盖(resident/gpu)叠加在最后,见 load_runtime_overrides。

    `apply_overrides=False` = **只要静态定义**(yaml 原样),给需要把「磁盘/yaml 基础结果」
    单独缓存、再在每次读时自己叠覆盖的调用方用(model_scanner.scan_models)——
    覆盖值绝不能被烘进那层 TTL 缓存,否则改了常驻/落卡最长 30s 看不到(见 scan_models 注释)。
    """
    resolved = _resolve_path(path)
    models = collect_model_entries(resolved)

    # New list-based format: convert to dict keyed by id
    if isinstance(models, list):
        result = {}
        for entry in models:
            model_id = entry["id"]
            # v2: `paths.main` is the canonical single-component path. Older
            # yaml using `path:` is not in the repo anymore (migrated by
            # PR-0 cutover) but we still gracefully read it as a fallback.
            paths = entry.get("paths") or {}
            # Single-component models (LLM/TTS) live under paths.main. Image
            # models are 3-component (transformer + text_encoder + vae) and
            # have no `main` — use the transformer's parent dir as the
            # canonical local_path so engines.py / scan_local_models can
            # match the entry against the on-disk directory.
            local_path = paths.get("main") or entry.get("path", "")
            if not local_path and paths.get("transformer"):
                from pathlib import Path as _P
                local_path = str(_P(paths["transformer"]).parent)
            result[model_id] = {
                "name": model_id,
                "type": entry.get("type", ""),
                # `gpu` stays None when unset so the GPU detector can auto-pick
                # a non-display card instead of defaulting to cuda:0.
                "gpu": entry.get("gpu"),
                # `gpus: [0, 2]` = 该模型以张量并行跨这组卡加载(见 src/gpu/topology.py)。
                # 与 `gpu` 并存,给了就以它为准。None = 未配。
                "gpus": entry.get("gpus"),
                "vram_gb": round(entry.get("vram_mb", 0) / 1024, 1),
                "resident": entry.get("resident", False),
                "local_path": local_path,
                "paths": paths,
                "ttl_seconds": entry.get("ttl_seconds", 300),
                # Preserve adapter so engines.py can compute has_adapter
                # without re-reading the yaml. Auto-detected entries fill
                # this same field from model_scanner.
                "adapter": entry.get("adapter"),
            }
            if entry.get("params"):
                result[model_id]["params"] = entry["params"]
            # V1' P2: optional `files{}` declares which on-disk files compose
            # this preset (transformer / text_encoder / vae). Lane C component
            # nodes consume it for dropdowns; the adapter still loads via paths.
            if entry.get("files"):
                result[model_id]["files"] = entry["files"]
        if apply_overrides:
            _apply_runtime_overrides(result)
        return result

    # Old dict-based format: return as-is
    if apply_overrides:
        _apply_runtime_overrides(models)
    return models


def recommend_vram_budget_gb(model_type: str, weights_gb: float) -> float:
    """每模型显存预算推荐值(GB)—— 权重 + 该模态典型激活/KV 余量(spec 2026-06-13)。
    embedding/tts 几乎不用 KV(单次前向)→ ×1.25;llm/vl 自回归解码要 KV → +6G 一档实用。
    其余(保守)×1.3。给 UI 显示「推荐」+ auto 落地参考。"""
    w = max(0.0, float(weights_gb or 0))
    t = (model_type or "").lower()
    if t in ("embedding", "tts"):
        rec = w * 1.25
    elif t in ("llm", "understand", "vl"):
        rec = w + 6.0
    else:
        rec = w * 1.3
    return round(max(1.0, rec), 1)


def resolve_vram_utilization(
    vram_budget: dict | None,
    gpu_total_gb: float,
    fallback: float | None,
    auto_util: float,
) -> float:
    """vram_budget({mode,value}) → vLLM gpu_memory_utilization(0–1)。
    优先级:显式 overlay vram_budget(percent/absolute)> models.yaml 的 fallback
    (gpu_memory_utilization)> auto 公式。mode=auto 或缺省 → 走 fallback/auto。
    absolute(GB)按目标卡真实总显存换算成比例;clamp 到 (0, 0.98]。"""
    if isinstance(vram_budget, dict):
        mode = str(vram_budget.get("mode") or "auto").lower()
        val = vram_budget.get("value")
        if mode == "percent" and isinstance(val, (int, float)) and val > 0:
            return max(0.01, min(0.98, float(val)))
        if mode == "absolute" and isinstance(val, (int, float)) and val > 0 and gpu_total_gb > 0:
            return max(0.01, min(0.98, float(val) / gpu_total_gb))
    if fallback:
        return max(0.01, min(0.98, float(fallback)))
    return auto_util


def _apply_runtime_overrides(cfgs: dict, copy_before_write: bool = False) -> None:
    """把运行时覆盖(resident/gpu/gpus/vram_budget)叠加进 cfgs(原地改)。overlay 优先于 models.yaml。

    `copy_before_write=True`:被覆盖到的那条 cfg 先浅拷贝再改,不写穿到原 dict —— 给
    「cfgs 是某个缓存结构的浅拷贝」的调用方用(model_scanner._with_runtime_overrides),
    否则覆盖值会渗进那层缓存、被 TTL 烘死。
    """
    overrides = load_runtime_overrides()
    for mid, ov in overrides.items():
        if mid in cfgs and isinstance(ov, dict):
            applied = {k: ov[k] for k in _OVERRIDABLE_KEYS if k in ov}
            if not applied:
                continue
            if copy_before_write:
                cfgs[mid] = {**cfgs[mid], **applied}
            else:
                cfgs[mid].update(applied)


@lru_cache
def load_hardware_config(path: str = "configs/hardware.yaml") -> dict:
    """Load the manual GPU topology config (hardware.yaml).

    Returns a dict with a "groups" list. fail-soft: missing file, corrupt
    YAML, or missing "groups" key all return {"groups": []} so the
    GPUAllocator can degrade to detect-based single-card groups instead
    of crashing API server startup (spec §3.2, manual-only topology).
    """
    # path may be absolute (tests) or relative-to-backend (default).
    candidate = Path(path)
    resolved = candidate if candidate.is_absolute() else _resolve_path(path)
    if not resolved.exists():
        return {"groups": []}
    try:
        with open(resolved) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return {"groups": []}
    groups = data.get("groups")
    if not isinstance(groups, list):
        return {"groups": []}
    return {"groups": groups}
