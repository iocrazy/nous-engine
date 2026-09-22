"""C1 回归:DB 里的 params 覆盖必须真的进到适配器构造参数。

这支之前整个特性是 no-op —— 端点写了库、GET 报"已生效",而 registry 的 spec.params
是 yaml 原样,适配器收到的还是老值。覆盖测试整齐地停在 load_model_configs() 这一层,
正好是缺口的上游,所以全绿也没碰到。

合并点刻意放在 `ModelManager._instantiate_adapter`(每次 load 跑)而不是
`ModelRegistry._load`(启动时跑一次、ModelSpec frozen)—— 否则 PATCH 之后必须重启
后端才生效,与端点「unload + load 生效」的承诺对不上。本文件的
`test_override_applies_without_rebuilding_registry` 就是钉这条。

**绝不起真子进程**:只调 `_instantiate_adapter`(构造适配器对象,不 load),
且适配器类换成假的。
"""
import pytest

from src.services import runtime_override_store

MODEL = "qwen3_8_27b_uncensored_fp8"


class _FakeAdapter:
    """假适配器:只记下收到的 kwargs。签名与真适配器同形(paths= + **kwargs)。"""

    supports_gpu_group = False

    def __init__(self, paths=None, **kwargs):
        self.paths = paths
        self.kwargs = kwargs


def _manager_with_fake_adapter(monkeypatch):
    """真 registry(读本机 models.d 的真 spec)+ 假适配器类。

    `_instantiate_adapter` 经 `importlib.import_module(...)` + `getattr` 拿类,
    所以把 manager 模块里的 `importlib` 换成一个只会吐出 _FakeAdapter 的替身。
    """
    import types

    from src.services.gpu_allocator import GPUAllocator
    from src.services import model_manager as mm_mod
    from src.services.inference.registry import ModelRegistry

    fake_module = types.SimpleNamespace()
    for attr in ("VLLMAdapter", "SGLangAdapter", "_FakeAdapter"):
        setattr(fake_module, attr, _FakeAdapter)
    monkeypatch.setattr(
        mm_mod.importlib, "import_module", lambda _path: fake_module, raising=True)

    registry = ModelRegistry("configs/models.yaml")
    return mm_mod.ModelManager(registry=registry, allocator=GPUAllocator()), registry


@pytest.mark.asyncio
async def test_runtime_params_override_reaches_adapter(db_session, monkeypatch):
    from src.config import load_model_configs

    if MODEL not in load_model_configs():
        pytest.skip(f"{MODEL} 不在本机 models.d")

    mgr, registry = _manager_with_fake_adapter(monkeypatch)
    spec = registry.get(MODEL)
    assert spec is not None
    # 前提:registry 的快照就是 yaml 原值(覆盖还没写)。
    assert spec.params.get("max_num_seqs") != 3

    await runtime_override_store.set_override(
        db_session, MODEL, "params", {"max_num_seqs": 3})

    adapter = mgr._instantiate_adapter(spec)
    assert adapter.kwargs.get("max_num_seqs") == 3, (
        f"运行时覆盖没进适配器:{adapter.kwargs.get('max_num_seqs')!r}")


@pytest.mark.asyncio
async def test_override_applies_without_rebuilding_registry(db_session, monkeypatch):
    """PATCH 之后**不重建 registry**(= 不重启后端)也要生效 —— 这正是合并点选在
    _instantiate_adapter 而非 _load 的理由。registry 在 set_override 之前就建好了。"""
    from src.config import load_model_configs

    if MODEL not in load_model_configs():
        pytest.skip(f"{MODEL} 不在本机 models.d")

    mgr, registry = _manager_with_fake_adapter(monkeypatch)
    spec = registry.get(MODEL)

    await runtime_override_store.set_override(
        db_session, MODEL, "params", {"max_model_len": 16384})
    assert mgr._instantiate_adapter(spec).kwargs["max_model_len"] == 16384

    # 再改一次,同一个 spec 对象要跟着变(每次 load 读的是 DB 当前值)。
    await runtime_override_store.set_override(
        db_session, MODEL, "params", {"max_model_len": 32768})
    assert mgr._instantiate_adapter(spec).kwargs["max_model_len"] == 32768


@pytest.mark.asyncio
async def test_non_whitelisted_override_key_never_reaches_adapter(db_session, monkeypatch):
    """白名单是**第二道**闸(写端点已经拦了一道)。放置/显存是放置结论,
    绝不能从 params 这条路渗回适配器 —— 库里要是有历史脏数据也得挡住。"""
    from src.config import load_model_configs

    if MODEL not in load_model_configs():
        pytest.skip(f"{MODEL} 不在本机 models.d")

    mgr, registry = _manager_with_fake_adapter(monkeypatch)
    spec = registry.get(MODEL)
    yaml_util = spec.params.get("gpu_memory_utilization")

    # 绕过端点直接写库(模拟历史脏数据 / 别的写入方)。
    await runtime_override_store.set_override(
        db_session, MODEL, "params",
        {"gpu_memory_utilization": 0.95, "tensor_parallel_size": 2, "max_num_seqs": 5})

    kwargs = mgr._instantiate_adapter(spec).kwargs
    assert kwargs.get("max_num_seqs") == 5, "白名单内的键该进去"
    assert kwargs.get("gpu_memory_utilization") == yaml_util, "util 被 params 覆盖渗透了"
    assert kwargs.get("tensor_parallel_size") == spec.params.get("tensor_parallel_size")


@pytest.mark.asyncio
async def test_prefix_caching_override_strips_vllm_args_alias(db_session, monkeypatch):
    """I3:关 prefix caching 时,vllm_args 里的同义写法必须一并摘掉。

    本机 qwen3.8 两个变体走的是 `params.vllm_args["enable-prefix-caching"]`,而
    merge_vllm_args 同名时以 vllm_args 为准 —— 不摘的话这个开关只开得了、关不掉。
    """
    from src.config import load_model_configs

    if MODEL not in load_model_configs():
        pytest.skip(f"{MODEL} 不在本机 models.d")

    mgr, registry = _manager_with_fake_adapter(monkeypatch)
    spec = registry.get(MODEL)
    va = spec.params.get("vllm_args") or {}
    if not any(a in va for a in ("enable-prefix-caching", "enable_prefix_caching")):
        pytest.skip(f"{MODEL} 的 yaml 没走 vllm_args 那条配法")

    await runtime_override_store.set_override(
        db_session, MODEL, "params", {"enable_prefix_caching": False})

    kwargs = mgr._instantiate_adapter(spec).kwargs
    assert kwargs["enable_prefix_caching"] is False
    out_va = kwargs.get("vllm_args") or {}
    assert "enable-prefix-caching" not in out_va
    assert "enable_prefix_caching" not in out_va
    # 没写穿 frozen spec 上那份(它与 registry 共享对象)。
    assert "enable-prefix-caching" in (spec.params.get("vllm_args") or {})
