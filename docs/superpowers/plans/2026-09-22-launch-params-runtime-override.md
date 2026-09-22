# 模型启动参数运行时覆盖 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `max_model_len` 等模型启动参数能经 API/UI 运行时覆盖并持久化到 DB,修复当前
`PATCH /engines/{name}/launch-params` 对任何模型都返回 404 的 bug。

**Architecture:** 给 `model_runtime_overrides` 加一个 `params` JSONB 列,只存被覆盖的键;
`_apply_runtime_overrides` 对 `params` 做**嵌套深合并**;端点改写 DB(不再写已作废的
`configs/models.yaml`);`gpu_memory_utilization` 与 `tensor_parallel_size` 移出白名单。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / Alembic / PostgreSQL JSONB /
React + TanStack Query / TypeScript

**Spec:** `docs/superpowers/specs/2026-09-22-launch-params-runtime-override-design.md`

## Global Constraints

- **测试只跑 PostgreSQL,没有 sqlite**。新加 `src/models/xxx.py` 必须同步加进
  `tests/conftest.py` 顶部的 import 列表(本计划不新增 model 文件,但改了既有 model)。
- **改 model 要「alembic 迁移 + micro-migration」双写**。当前 alembic head:`d7a4b1e6c093`。
- **测试绝不能真起推理服务碰 GPU**。conftest 有 Popen 护栏。本计划全程不需要起 vLLM。
- **别用 `NOUS_TEST_USE_REAL_DB=1`**(会污染生产数据)。
- 并行跑测试:`uv run pytest tests -n 8`。全量基线:`2230 passed, 8 skipped`
  (不带 `--ignore=tests/integration`;带则少 20 条)。
- 前端构建是 `npm run build` = `prebuild`(需 `wasm-pack`)+ `tsc -b && vite build`。
  **不能用裸 `npx tsc` 替代 `tsc -b`**(不认 composite 引用会报一堆假错误)。
- 所有新写注释/文案用中文,与既有风格一致。

---

### Task 1: DB 列 + 模型字段 + 双写迁移

**Files:**
- Modify: `backend/src/models/model_runtime_override.py`
- Create: `backend/alembic/versions/20260922_f3b8c1d4e207_add_model_runtime_overrides_params.py`
- Modify: `backend/src/api/main.py:52`(`_MICRO_MIGRATIONS` 元组)
- Test: `backend/tests/test_runtime_override_params.py`

**Interfaces:**
- Produces:`ModelRuntimeOverride.params`(JSONB,nullable);
  `to_overrides()` 在 `params` 非空时输出 `{"params": {...}}`。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_runtime_override_params.py`:

```python
"""model_runtime_overrides.params —— 启动参数的运行时覆盖。

三态(与 gpus 的 `[]` 哨兵同源的教训):
  NULL / {}  = 未覆盖 → 回退 models.d yaml 的 params
  {"k": v}   = 只覆盖 k,其余键仍走 yaml
"""
from src.models.model_runtime_override import ModelRuntimeOverride


def test_to_overrides_emits_params_when_set():
    row = ModelRuntimeOverride(model_id="m", params={"max_model_len": 262144})
    assert row.to_overrides() == {"params": {"max_model_len": 262144}}


def test_to_overrides_omits_empty_params():
    """空 dict 与 NULL 都算「没覆盖」——不像 gpus,params 没有「显式清空」的语义需求:
    要清空就是把键删掉,整列回到 NULL。"""
    assert ModelRuntimeOverride(model_id="m", params={}).to_overrides() == {}
    assert ModelRuntimeOverride(model_id="m", params=None).to_overrides() == {}


def test_to_overrides_params_coexists_with_placement():
    row = ModelRuntimeOverride(model_id="m", gpu=1, params={"max_num_seqs": 8})
    out = row.to_overrides()
    assert out["gpu"] == 1
    assert out["params"] == {"max_num_seqs": 8}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_runtime_override_params.py -q`
Expected: FAIL —— `TypeError: 'params' is an invalid keyword argument`

- [ ] **Step 3: 加模型字段**

在 `backend/src/models/model_runtime_override.py` 的 `vram_budget_value` 之后、
`updated_at` 之前插入:

```python
    # 启动参数覆盖(spec 2026-09-22)。只存**被覆盖的键**,其余回退 models.d 的 params。
    #   NULL / {} = 未覆盖;{"max_model_len": 262144} = 只覆盖这一个键
    # 不需要 gpus 那种 `[]` 哨兵:params 是 dict,清某个键就是把它从 dict 里删掉,
    # 全清就是整列回 NULL —— 不存在「显式清空」与「未设置」语义冲突。
    # JSONB 而非 typed 多列:白名单会随 vLLM 版本增减,用 dict 才不用每次改 schema。
    params = Column(JSONB, nullable=True)
```

在 `to_overrides()` 的 `return out` 之前插入:

```python
        if self.params:
            out["params"] = dict(self.params)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_runtime_override_params.py -q`
Expected: PASS(3 passed)

- [ ] **Step 5: 写 alembic 迁移**

创建 `backend/alembic/versions/20260922_f3b8c1d4e207_add_model_runtime_overrides_params.py`:

```python
"""add model_runtime_overrides.params

模型启动参数的运行时覆盖(spec 2026-09-22-launch-params-runtime-override)。
只存被覆盖的键:{"max_model_len": 262144}。NULL = 未覆盖,回退 models.d 的 params。

JSONB 而非 typed 多列:可覆盖的参数白名单会随 vLLM 版本增减,用 dict 才不用每次改 schema。

Revision ID: f3b8c1d4e207
Revises: d7a4b1e6c093
Create Date: 2026-09-22 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f3b8c1d4e207'
down_revision: Union[str, None] = 'd7a4b1e6c093'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'model_runtime_overrides',
        sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('model_runtime_overrides', 'params')
```

- [ ] **Step 6: 加 micro-migration(双写)**

在 `backend/src/api/main.py` 的 `_MICRO_MIGRATIONS` 元组末尾(闭合括号 `)` 之前)加:

```python
    # 启动参数覆盖(spec 2026-09-22)。与 alembic f3b8c1d4e207 双写 —— 没跑 alembic 的
    # 环境(测试临时库 / 老部署)也能有这一列。
    "ALTER TABLE model_runtime_overrides ADD COLUMN IF NOT EXISTS params JSONB",
```

- [ ] **Step 7: 验证迁移往返**

Run:
```bash
cd backend && uv run alembic upgrade head && uv run alembic current
```
Expected: 输出含 `f3b8c1d4e207 (head)`

Run:
```bash
cd backend && uv run alembic downgrade -1 && uv run alembic current && uv run alembic upgrade head
```
Expected: downgrade 后 current 是 `d7a4b1e6c093`,再 upgrade 回 `f3b8c1d4e207 (head)`,全程无报错。

- [ ] **Step 8: 提交**

```bash
git add backend/src/models/model_runtime_override.py backend/alembic/versions/ backend/src/api/main.py backend/tests/test_runtime_override_params.py
git commit -m "feat(db): model_runtime_overrides 加 params 列 —— 启动参数的运行时覆盖"
```

---

### Task 2: 覆盖写入(store 支持 params 键)

**Files:**
- Modify: `backend/src/services/runtime_override_store.py:18`(`VALID_KEYS`)、`:55`(`set_override`)
- Test: `backend/tests/test_runtime_override_params.py`(追加)

**Interfaces:**
- Consumes:Task 1 的 `ModelRuntimeOverride.params`
- Produces:`await set_override(session, model_id, "params", {"max_model_len": 262144})`;
  值里某键为 `None` → **删除该键**;传 `{}` 或 `None` → 整列清空

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_runtime_override_params.py`:

```python
import pytest

from src.services import runtime_override_store


@pytest.mark.asyncio
async def test_set_override_params_writes_and_merges(db_session):
    await runtime_override_store.set_override(
        db_session, "m1", "params", {"max_model_len": 262144})
    assert runtime_override_store.get_overrides()["m1"]["params"] == {
        "max_model_len": 262144}

    # 再写第二个键:**合并**而不是替换整个 dict
    await runtime_override_store.set_override(
        db_session, "m1", "params", {"max_num_seqs": 8})
    assert runtime_override_store.get_overrides()["m1"]["params"] == {
        "max_model_len": 262144, "max_num_seqs": 8}


@pytest.mark.asyncio
async def test_set_override_params_none_value_deletes_key(db_session):
    await runtime_override_store.set_override(
        db_session, "m2", "params", {"max_model_len": 262144, "max_num_seqs": 8})
    # 显式 None = 删这个键(回退 yaml),不是"把它设成 null"
    await runtime_override_store.set_override(
        db_session, "m2", "params", {"max_num_seqs": None})
    assert runtime_override_store.get_overrides()["m2"]["params"] == {
        "max_model_len": 262144}


@pytest.mark.asyncio
async def test_set_override_params_empty_clears_column(db_session):
    await runtime_override_store.set_override(
        db_session, "m3", "params", {"max_model_len": 262144})
    await runtime_override_store.set_override(db_session, "m3", "params", {})
    assert "params" not in runtime_override_store.get_overrides().get("m3", {})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_runtime_override_params.py -q`
Expected: FAIL —— `ValueError: non-overridable key: 'params'`

- [ ] **Step 3: 实现**

`backend/src/services/runtime_override_store.py:18` 改为:

```python
VALID_KEYS = ("resident", "gpu", "gpus", "vram_budget", "params")
```

在 `set_override` 的 `elif key == "vram_budget":` 分支之后、`await session.commit()` 之前插入:

```python
    elif key == "params":
        # value = {"max_model_len": 262144} → **合并**进既有 dict(不是整体替换):
        # 端点每次只传用户改动的那几个键,整体替换会把之前设过的其它键冲掉。
        # 某键的值为 None = **删除该键**(回退 models.d 的 yaml 值);
        # 整个 value 为空({} / None)= 清空整列。
        if not value:
            row.params = None
        else:
            merged = dict(row.params or {})
            for k, v in value.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            row.params = merged or None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_runtime_override_params.py -q`
Expected: PASS(6 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/src/services/runtime_override_store.py backend/tests/test_runtime_override_params.py
git commit -m "feat(store): set_override 支持 params —— 按键合并,None 删键,空值清列"
```

---

### Task 3: 嵌套深合并(含 copy_before_write 陷阱)

**Files:**
- Modify: `backend/src/config.py:326`(`_apply_runtime_overrides`)
- Test: `backend/tests/test_runtime_override_params.py`(追加)

**Interfaces:**
- Consumes:Task 2 写出的 `{"params": {...}}` 形状
- Produces:`load_model_configs()` 返回的 cfg 里 `params` 是 yaml 与覆盖的**深合并**结果

**⚠️ 本任务的核心是一个隐蔽陷阱,不是普通合并:**
`copy_before_write=True` 那条路(给 `model_scanner._with_runtime_overrides` 的 TTL 缓存用)
只做了 cfg 的**浅**拷贝,`params` 子 dict 仍是共享引用。若直接
`cfgs[mid]["params"].update(...)`,会**写穿进 TTL 缓存** —— 正是该函数 docstring
警告过的「覆盖值渗进缓存、被 TTL 烘死」的镜像问题。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_runtime_override_params.py`:

```python
from src.config import _apply_runtime_overrides


def test_params_deep_merge_keeps_unoverridden_keys(monkeypatch):
    """只覆盖 max_model_len,max_num_seqs 必须仍是 yaml 的值(不能整体替换 params)。"""
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"max_model_len": 262144}}})
    cfgs = {"m": {"id": "m", "params": {"max_model_len": 32768, "max_num_seqs": 16}}}
    _apply_runtime_overrides(cfgs)
    assert cfgs["m"]["params"] == {"max_model_len": 262144, "max_num_seqs": 16}


def test_params_merge_does_not_write_through_cache(monkeypatch):
    """copy_before_write=True 时**绝不能**改到原 params dict。

    调用方(model_scanner._with_runtime_overrides)传进来的是某个 TTL 缓存结构的浅拷贝:
    外层 dict 是新的,但 params 子 dict 与缓存共享同一个对象。写穿的症状很阴 ——
    「改了参数 30 秒内看不到」或「改一次污染此后所有读」,难查。
    """
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"max_model_len": 262144}}})
    cached_params = {"max_model_len": 32768, "max_num_seqs": 16}
    cached_cfg = {"id": "m", "params": cached_params}
    shallow = {"m": dict(cached_cfg)}          # 模拟调用方的浅拷贝

    _apply_runtime_overrides(shallow, copy_before_write=True)

    assert shallow["m"]["params"]["max_model_len"] == 262144, "覆盖没生效"
    assert cached_params["max_model_len"] == 32768, "写穿了缓存里的原 params dict"


def test_params_override_on_cfg_without_params_key(monkeypatch):
    """yaml 里没有 params 块的模型也要能被覆盖(别 KeyError)。"""
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"max_model_len": 4096}}})
    cfgs = {"m": {"id": "m"}}
    _apply_runtime_overrides(cfgs)
    assert cfgs["m"]["params"] == {"max_model_len": 4096}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_runtime_override_params.py -q -k params_`
Expected: FAIL —— 深合并未实现,`params` 仍是 yaml 原值

- [ ] **Step 3: 实现**

把 `backend/src/config.py` 的 `_apply_runtime_overrides` 函数体(`overrides = load_runtime_overrides()`
之后的 for 循环)整体替换为:

```python
    overrides = load_runtime_overrides()
    for mid, ov in overrides.items():
        if mid in cfgs and isinstance(ov, dict):
            applied = {k: ov[k] for k in _OVERRIDABLE_KEYS if k in ov}
            # params 是**嵌套**覆盖,不能进 _OVERRIDABLE_KEYS 那套浅合并 ——
            # 那会用覆盖里的几个键**整体替换**掉 yaml 的 params 块。
            ov_params = ov.get("params") or {}
            if not applied and not ov_params:
                continue
            if copy_before_write:
                merged = {**cfgs[mid], **applied}
                if ov_params:
                    # ⚠️ 连 params 一起**新建 dict**。只浅拷外层的话,params 子 dict
                    # 与调用方的 TTL 缓存共享同一对象,原地改会写穿(见 docstring)。
                    merged["params"] = {**(cfgs[mid].get("params") or {}), **ov_params}
                cfgs[mid] = merged
            else:
                cfgs[mid].update(applied)
                if ov_params:
                    # 这里也是**赋一个新 dict**,不是 .update() 原 dict —— 保持与上面
                    # 同样的「不原地改 params」语义,免得两条分支行为分叉。
                    cfgs[mid]["params"] = {
                        **(cfgs[mid].get("params") or {}), **ov_params}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_runtime_override_params.py -q`
Expected: PASS(9 passed)

- [ ] **Step 5: 跑全量确认没连带破坏**

Run: `cd backend && uv run pytest tests -n 8 -q`
Expected: `2239 passed, 8 skipped`(基线 2230 + 本文件 9 条)

- [ ] **Step 6: 提交**

```bash
git add backend/src/config.py backend/tests/test_runtime_override_params.py
git commit -m "feat(config): params 覆盖走嵌套深合并 —— 并堵住 copy_before_write 写穿缓存"
```

---

### Task 4: 端点改写 DB + 白名单收紧

**Files:**
- Modify: `backend/src/api/routes/engines.py:837`(`set_launch_params`)
- Test: `backend/tests/test_launch_params_endpoint.py`

**Interfaces:**
- Consumes:Task 2 的 `set_override(session, name, "params", {...})`
- Produces:`PATCH /api/v1/engines/{name}/launch-params`
  → `{"name":..., "params": {...}, "applied": false, "hint": "unload + load 生效"}`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_launch_params_endpoint.py`:

```python
"""PATCH /engines/{name}/launch-params —— 启动参数运行时覆盖。

用 `db_client` fixture(需要真 DB 写 override);**不传 admin headers** ——
conftest.py:84 强制 `ADMIN_PASSWORD=""`,整个测试套件里 admin 闸门是关的。

2026-09-22 之前这个端点是**坏的**:它写 configs/models.yaml,而模型定义 2026-06-20
已迁到 models.d/,那文件只剩 `models: []` 空锚点 → 遍历空列表 → 对**任何**模型都 404。
零测试覆盖,所以坏了没人发现。本文件就是补这个网。
"""
import pytest


@pytest.mark.asyncio
async def test_patch_launch_params_persists(db_client):
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"max_model_len": 16384})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["params"]["max_model_len"] == 16384
    assert body["applied"] is False   # 参数只在下次 load 时读

    from src.services import runtime_override_store
    assert runtime_override_store.get_overrides()[
        "qwen3_8_27b_abliterated_awq"]["params"]["max_model_len"] == 16384


@pytest.mark.asyncio
async def test_patch_rejects_gpu_memory_utilization(db_client):
    """util 是「占该卡总量」的比例,换卡必须重算 —— 把它做成按钮就是把坑做成按钮。
    显存要改走 vram-budget(绝对 GiB,加载时按实际那张卡换算)。"""
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"gpu_memory_utilization": 0.9})
    assert r.status_code == 400
    assert "vram-budget" in r.text


@pytest.mark.asyncio
async def test_patch_rejects_tensor_parallel_size(db_client):
    """tp 是**放置结论**(_placement 定),不是调优旋钮。"""
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"tensor_parallel_size": 2})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_rejects_unknown_key(db_client):
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"nonsense_flag": 1})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_unknown_engine_404(db_client):
    r = await db_client.patch(
        "/api/v1/engines/no_such_model/launch-params",
        json={"max_model_len": 4096})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_null_clears_single_key(db_client):
    name = "qwen3_8_27b_abliterated_awq"
    await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                       json={"max_model_len": 16384, "max_num_seqs": 4})
    r = await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                              json={"max_num_seqs": None})
    assert r.status_code == 200
    assert r.json()["params"] == {"max_model_len": 16384}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_launch_params_endpoint.py -q`
Expected: FAIL —— 第一条 404(端点写空 models.yaml),拒绝类的也没实现

- [ ] **Step 3: 重写端点**

把 `backend/src/api/routes/engines.py` 的整个 `set_launch_params` 函数
(`@router.patch("/{name}/launch-params", ...)` 到它 `return` 为止)替换为:

```python
#: 可运行时覆盖的启动参数。**刻意不含** gpu_memory_utilization 与 tensor_parallel_size:
#:   - util 是「占该卡总量」的比例,换卡必须重算(2026-09-11 事故:改落卡忘改 util,
#:     模型在 Pro 6000 上抓 53.3GiB 把 ComfyUI 挤到 19GiB)。显存走 vram-budget ——
#:     它存绝对 GiB,在**加载时**按实际那张卡换算(llm_vllm.py 的预算优先级)。
#:   - tp 是**放置结论**(_placement 定),不是调优旋钮。
_LAUNCH_PARAM_WHITELIST = frozenset({
    "max_model_len",
    "max_num_seqs",
    "max_num_batched_tokens",
    "enable_prefix_caching",
    "dtype",
    "quantization",
})

#: 这些键单独给理由,不要混在"不在白名单"的通用报错里 —— 用户会以为是拼错了。
_LAUNCH_PARAM_REDIRECTS = {
    "gpu_memory_utilization":
        "显存预算请改用 PATCH /engines/{name}/vram-budget(mode=absolute + 绝对 GiB)。"
        "gpu_memory_utilization 是「占该卡总量」的比例,换卡必须重算,不作为可编辑项。",
    "tensor_parallel_size":
        "tp 由放置决定(ModelManager._resolve_placement),不是调优旋钮。"
        "要换卡/换组请用 PATCH /engines/{name}/gpu。",
}


@router.patch("/{name}/launch-params", dependencies=[Depends(require_admin)])
async def set_launch_params(
    name: str,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    """覆盖模型的启动参数,持久到 DB 的 model_runtime_overrides.params。

    **不写 configs/models.yaml** —— 2026-06-20 起模型定义在 models.d/,那个文件只剩
    空锚点;而且写 git 跟踪的 yaml 会被 git checkout/pull 冲掉(同 resident 端点的理由)。

    值为 `null` 的键 = **清除该覆盖**(回退 models.d 的 yaml 值)。
    改动只在**下次 load** 时读到,所以 `applied` 恒为 false。
    """
    from src.config import load_model_configs
    from src.services import runtime_override_store

    if name not in load_model_configs():
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    for k, hint in _LAUNCH_PARAM_REDIRECTS.items():
        if k in body:
            raise HTTPException(400, detail=hint.format(name=name))

    bad = [k for k in body if k not in _LAUNCH_PARAM_WHITELIST]
    if bad:
        raise HTTPException(
            400,
            detail=f"不可覆盖的参数 {bad};允许:{sorted(_LAUNCH_PARAM_WHITELIST)}",
        )
    if not body:
        raise HTTPException(400, detail="body 为空;至少给一个参数")

    await runtime_override_store.set_override(session, name, "params", body)

    invalidate("engines")
    merged = runtime_override_store.get_overrides().get(name, {}).get("params", {})
    return {
        "name": name,
        "params": merged,
        "applied": False,
        "hint": "需重新加载模型生效(unload + load)",
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_launch_params_endpoint.py -q`
Expected: PASS(6 passed)

- [ ] **Step 5: 跑全量**

Run: `cd backend && uv run pytest tests -n 8 -q`
Expected: `2245 passed, 8 skipped`

- [ ] **Step 6: 提交**

```bash
git add backend/src/api/routes/engines.py backend/tests/test_launch_params_endpoint.py
git commit -m "fix(api): launch-params 改写 DB 覆盖 —— 修 404,并把 util/tp 移出白名单"
```

---

### Task 5: 前端参数编辑器

**Files:**
- Modify: `frontend/src/api/vllm.ts:45-68`(`LaunchParamsBody` 与 hook)
- Create: `frontend/src/components/engines/LaunchParamsEditor.tsx`
- Modify: `frontend/src/components/overlays/DashboardOverlay.tsx:1620-1650`(移除孤立复选框)

**Interfaces:**
- Consumes:Task 4 的 `PATCH /engines/{name}/launch-params`
- Produces:`<LaunchParamsEditor engineName={string} current={Record<string, unknown>} />`

- [ ] **Step 1: 收紧 TS 类型**

把 `frontend/src/api/vllm.ts` 的 `LaunchParamsBody` 定义(含 `enable_prefix_caching`
那几行直到闭合 `}`)替换为:

```ts
// 与后端 _LAUNCH_PARAM_WHITELIST 一一对应。
// **刻意不含 gpu_memory_utilization / tensor_parallel_size** —— 后端会 400:
//   显存走 vram-budget(绝对 GiB,加载时按实际那张卡换算);tp 由放置决定。
// null = 清除该覆盖,回退 models.d 的 yaml 值。
export type LaunchParamsBody = {
  max_model_len?: number | null
  max_num_seqs?: number | null
  max_num_batched_tokens?: number | null
  enable_prefix_caching?: boolean | null
  dtype?: string | null
  quantization?: string | null
}
```

- [ ] **Step 2: 写编辑器组件**

创建 `frontend/src/components/engines/LaunchParamsEditor.tsx`:

```tsx
import { useState } from 'react'
import { useUpdateLaunchParams, type LaunchParamsBody } from '../../api/vllm'

// 常用上下文档位。256K = Qwen3.8 系列原生上限(max_position_embeddings 262144)。
const CTX_PRESETS = [
  { label: '32K', value: 32768 },
  { label: '128K', value: 131072 },
  { label: '256K', value: 262144 },
]

export function LaunchParamsEditor({
  engineName,
  current,
}: {
  engineName: string
  current: Record<string, unknown>
}) {
  const update = useUpdateLaunchParams()
  const [draft, setDraft] = useState<LaunchParamsBody>({})

  const save = (patch: LaunchParamsBody) =>
    update.mutate({ name: engineName, body: patch })

  const num = (k: keyof LaunchParamsBody) =>
    (draft[k] as number | undefined) ?? (current[k] as number | undefined) ?? ''

  return (
    <div style={{ display: 'grid', gap: 10, fontSize: 12 }}>
      <div>
        <label style={{ display: 'block', marginBottom: 4 }}>
          上下文长度 (max_model_len)
        </label>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input
            type="number"
            value={num('max_model_len')}
            onChange={(e) =>
              setDraft({ ...draft, max_model_len: Number(e.target.value) })
            }
            style={{ width: 110 }}
          />
          {CTX_PRESETS.map((p) => (
            <button
              key={p.value}
              type="button"
              disabled={update.isPending}
              onClick={() => save({ max_model_len: p.value })}
            >
              {p.label}
            </button>
          ))}
        </div>
        <p style={{ color: 'var(--muted)', marginTop: 4 }}>
          上下文与并发此消彼长:同样的 KV 池,长度减半则并发翻倍。
          调大可能因 KV 装不下而**起不来**(vLLM 启动时会拒绝)。
        </p>
      </div>

      <label>
        最大并发序列 (max_num_seqs)
        <input
          type="number"
          value={num('max_num_seqs')}
          onChange={(e) =>
            setDraft({ ...draft, max_num_seqs: Number(e.target.value) })
          }
          style={{ width: 90, marginLeft: 6 }}
        />
      </label>
      <p style={{ color: 'var(--muted)', marginTop: -6 }}>
        这是**调度上限,不是并发驱动力** —— 真实并发由 KV 池决定,只调大它不给 KV 没有提升。
      </p>

      <label>
        单批最大 token (max_num_batched_tokens)
        <input
          type="number"
          value={num('max_num_batched_tokens')}
          onChange={(e) =>
            setDraft({
              ...draft,
              max_num_batched_tokens: Number(e.target.value),
            })
          }
          style={{ width: 90, marginLeft: 6 }}
        />
      </label>

      <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <input
          type="checkbox"
          checked={Boolean(
            draft.enable_prefix_caching ?? current.enable_prefix_caching,
          )}
          disabled={update.isPending}
          onChange={(e) => save({ enable_prefix_caching: e.target.checked })}
        />
        Prefix Caching(共享 system prompt 时跳过重复 prefill)
      </label>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button
          type="button"
          disabled={update.isPending || Object.keys(draft).length === 0}
          onClick={() => {
            save(draft)
            setDraft({})
          }}
        >
          保存
        </button>
        <span style={{ color: 'var(--muted)' }}>
          {update.isPending
            ? '保存中…'
            : '改后需 unload + load 才生效'}
        </span>
      </div>

      <p style={{ color: 'var(--muted)', borderTop: '1px solid var(--border)', paddingTop: 8 }}>
        显存预算不在这里 —— 走「显存预算」那栏(绝对 GiB)。
        <code>gpu_memory_utilization</code> 是「占该卡总量」的比例,换卡必须重算,
        所以不作为可编辑项。
      </p>
    </div>
  )
}
```

- [ ] **Step 3: 移除 DashboardOverlay 里的孤立复选框**

在 `frontend/src/components/overlays/DashboardOverlay.tsx` 中删除 `prefixOn` 那个
`<label>…Prefix Caching</label>` 整块(约 :1624-1646)及其外层 `<div>` 包装,
并删除 `const update = useUpdateLaunchParams()`(:1488)与 `const prefixOn = ...`(:1509)
两行,以及 `import` 里的 `useUpdateLaunchParams`。

理由写进被删位置上方的注释:

```tsx
// 启动参数编辑移到引擎页的 LaunchParamsEditor —— 两处编辑同一份状态会打架,
// 且这些是**模型级**参数,不属于 Dashboard 的实例监控面板。
```

- [ ] **Step 4: 类型检查与构建**

Run: `cd frontend && npx tsc -b`
Expected: 无错误(**必须用 `tsc -b`**,裸 `npx tsc` 不认 composite 引用会报假错误)

Run: `cd frontend && npm run build`
Expected: 构建成功

- [ ] **Step 5: 前端测试**

Run: `cd frontend && npm test -- DashboardOverlay`
Expected: PASS(`DashboardOverlay.test.tsx` 里 mock 了 `useUpdateLaunchParams`;
若因移除 import 而报未使用的 mock,一并清掉该 mock 行)

- [ ] **Step 6: 提交**

```bash
git add frontend/src
git commit -m "feat(ui): 引擎页新增启动参数编辑器,移除 Dashboard 里的孤立复选框"
```

---

### Task 6: 文档同步

**Files:**
- Modify: `CLAUDE.md`(「vLLM 参数透传」节之后)

- [ ] **Step 1: 补一节**

在 `CLAUDE.md` 的「## vLLM 参数透传 (`params.vllm_args`)」整节之后插入:

```markdown
## 启动参数的运行时覆盖

- `PATCH /api/v1/engines/{name}/launch-params` 把启动参数写进 DB 的
  `model_runtime_overrides.params`(JSONB,只存被覆盖的键),**不写 yaml** ——
  写 git 跟踪的文件会被 `git checkout/pull` 冲掉(同 `resident` 端点的理由)。
  值为 `null` = 清除该覆盖回退 yaml。改动**下次 load 才生效**(`applied` 恒 false)。
- 白名单:`max_model_len` / `max_num_seqs` / `max_num_batched_tokens` /
  `enable_prefix_caching` / `dtype` / `quantization`。
- **`gpu_memory_utilization` 与 `tensor_parallel_size` 刻意不可覆盖**,PATCH 到会 400:
  util 是「占该卡总量」的比例、换卡必须重算(2026-09-11 事故:改落卡忘改 util,
  模型在 Pro 6000 上抓 53.3GiB 把 ComfyUI 挤到 19GiB),显存一律走
  `PATCH /engines/{name}/vram-budget`(存**绝对 GiB**,加载时按实际那张卡换算);
  tp 是放置结论,由 `_resolve_placement` 定。
- 合并是**嵌套深合并**(`config.py::_apply_runtime_overrides`):只覆盖给定的键,
  其余仍走 models.d 的 `params`。⚠️ `copy_before_write=True` 时必须**连 `params`
  一起新建 dict** —— 只浅拷外层的话,`params` 子 dict 与 `model_scanner` 的 TTL
  缓存共享同一对象,原地改会写穿(症状:改了 30 秒看不到,或改一次污染此后所有读)。
```

- [ ] **Step 2: 提交**

```bash
git add CLAUDE.md
git commit -m "docs(claude): 补「启动参数的运行时覆盖」一节"
```
