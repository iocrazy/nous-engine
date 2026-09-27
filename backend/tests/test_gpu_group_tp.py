"""模型级 GPU 组(张量并行)—— 拓扑权威、放置决策、落卡、覆盖三态、API。

背景:机器上 cuda:0 与 cuda:2 是一对 NVLink 直连的 3090(24G × 2),cuda:1 是
RTX PRO 6000(96G)。老逻辑「装不下单卡 → tp = len(gpu_stats)」会把三张异构卡一起
拉进张量并行,且 tp>1 时压根不钉 `CUDA_VISIBLE_DEVICES`。

本文件钉死返修后的规则:
  * 候选组的**权威来源是 hardware.yaml**(`GPUAllocator` 解析的那份),不是枚举卡的组合;
  * **放置决策只在 `ModelManager._resolve_placement`**,适配器只执行;
  * 显式 `gpu`/`gpus` 是**硬约束**,放不下就 raise,不自动搬;
  * **绝不存在"不钉卡"的启动分支**;
  * 覆盖里的 `gpus: []` 是「显式清空组」的哨兵。

CI 安全:全程 mock,**绝不真起 vLLM / 碰 GPU**(conftest 另有 Popen 护栏)。
"""
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.gpu.topology import (
    candidate_groups,
    group_budget_gb,
    group_is_nvlinked,
    parse_topo_matrix,
    resolve_gpus,
    select_tp_group,
    validate_gpu_group,
)
from src.gpu.topology import nvlink_pairs as _real_nvlink_pairs
from src.services.gpu_allocator import GPUGroup

# ---------------------------------------------------------------------------
# 测试用的机器画像:0/2 = 3090(NVLink 对),1 = PRO 6000
# ---------------------------------------------------------------------------
_NAMES = {
    0: "NVIDIA GeForce RTX 3090",
    1: "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
    2: "NVIDIA GeForce RTX 3090",
}
_TOTALS = {0: 24.0, 1: 96.0, 2: 24.0}
_PAIRS = {frozenset((0, 2))}
# hardware.yaml 声明了一个 NVLink 3090 对 —— 候选组的唯一来源。
_TP_GROUP = GPUGroup(id="llm-tp", gpus=[0, 2], nvlink=True, role="llm", vram_gb=48)
_SINGLE_GROUPS = [GPUGroup(id="llm", gpus=[1], nvlink=False, role="llm", vram_gb=96)]


def _stats(free_by_idx: dict[int, int], total_by_idx: dict[int, int] | None = None):
    total_by_idx = total_by_idx or {0: 24576, 1: 98304, 2: 24576}
    return [
        {"index": i, "free_mb": free_by_idx[i], "total_mb": total_by_idx[i]}
        for i in sorted(free_by_idx)
    ]


@pytest.fixture(autouse=True)
def _fake_gpu_env():
    """所有用例都跑在「0/2 是 NVLink 3090 对、1 是 Pro 6000、没有显示卡」的假机器上。"""
    from src.gpu import topology

    topology.reset_cache()
    # /api/v1/gpu/groups 走 @cached("gpu-groups") —— 用例之间必须清,否则上一条的
    # 结果会漏给下一条。
    from src.api.response_cache import invalidate

    invalidate("gpu-groups")
    with patch.object(topology, "gpu_names", return_value=_NAMES), \
            patch.object(topology, "gpu_totals_gb", return_value=_TOTALS), \
            patch.object(topology, "display_gpu_indices", return_value=set()), \
            patch.object(topology, "nvlink_pairs", return_value=_PAIRS), \
            patch.object(topology, "hardware_groups", return_value=[_TP_GROUP]):
        yield
    invalidate("gpu-groups")
    topology.reset_cache()


# ---------------------------------------------------------------------------
# nvidia-smi topo -m 解析(只用于 nvlink 的校验/补缺)
# ---------------------------------------------------------------------------

_TOPO = """\t\x1b[4mGPU0\tGPU1\tGPU2\tCPU Affinity\tNUMA Affinity\tGPU NUMA ID\x1b[0m
GPU0\t X \tNODE\tNV4\t0-47\t0\t\tN/A
GPU1\tNODE\t X \tPHB\t0-47\t0\t\tN/A
GPU2\tNV4\tPHB\t X \t0-47\t0\t\tN/A

Legend:
  X    = Self
"""


def test_parse_topo_matrix_finds_nvlink_pair():
    assert parse_topo_matrix(_TOPO) == {frozenset((0, 2))}


def test_parse_topo_matrix_ignores_pcie_links():
    pairs = parse_topo_matrix(_TOPO)
    assert frozenset((0, 1)) not in pairs
    assert frozenset((1, 2)) not in pairs


def test_parse_topo_matrix_survives_garbage():
    assert parse_topo_matrix("") == set()
    assert parse_topo_matrix("no gpus here\n") == set()


def test_group_is_nvlinked_requires_every_pair():
    assert group_is_nvlinked([0, 2], _PAIRS) is True
    assert group_is_nvlinked([0, 1], _PAIRS) is False
    assert group_is_nvlinked([0, 1, 2], _PAIRS) is False
    assert group_is_nvlinked([0], _PAIRS) is False


def test_failed_topo_probe_is_not_cached_forever():
    """审查 #18:探测失败只压 30s 负缓存(不是永久),重试能拿到真值。

    `_real_nvlink_pairs` 是模块 import 时抓的原始函数 —— autouse fixture 把
    `topology.nvlink_pairs` 打了桩,这里要的是真实现。
    """
    from src.gpu import topology

    topology.reset_cache()
    with patch("subprocess.run", MagicMock(side_effect=OSError("nvidia-smi timeout"))):
        assert _real_nvlink_pairs() == set()
        # 负缓存有到期时间,不是"永久空集"
        expires, cached = topology._nvlink_cache
        assert cached == set() and expires != float("inf")
    with patch("subprocess.run",
               MagicMock(return_value=MagicMock(returncode=0, stdout=_TOPO))):
        assert _real_nvlink_pairs(refresh=True) == {frozenset((0, 2))}
        assert topology._nvlink_cache[0] == float("inf")  # 成功才长期缓存
    topology.reset_cache()


# ---------------------------------------------------------------------------
# gpu / gpus 优先级(唯一实现)
# ---------------------------------------------------------------------------

def test_resolve_gpus_group_wins_over_single():
    assert resolve_gpus({"gpu": 1, "gpus": [0, 2]}) == [0, 2]


def test_resolve_gpus_empty_group_is_explicit_clear():
    """`gpus: []` 是「显式清空组」的哨兵 → 回落到单卡 gpu(审查 #9)。"""
    assert resolve_gpus({"gpu": 1, "gpus": []}) == [1]


def test_resolve_gpus_handles_legacy_shapes():
    assert resolve_gpus({"gpu": 0}) == [0]
    assert resolve_gpus({"gpu": [1, 2]}) == [1, 2]          # 历史 list 形状
    assert resolve_gpus({"gpu": "cuda:2"}) == [2]
    assert resolve_gpus({}) == []
    assert resolve_gpus(None) == []


def test_resolve_gpus_reads_model_spec_objects():
    from src.services.inference.registry import ModelSpec

    spec = ModelSpec(id="m", model_type="llm", adapter_class="x.Y",
                     paths={"main": "m"}, vram_mb=1024, gpus=[0, 2])
    assert resolve_gpus(spec) == [0, 2]


# ---------------------------------------------------------------------------
# 组校验(HTTP 与 YAML 路径共用的唯一实现)
# ---------------------------------------------------------------------------

def test_validate_rejects_heterogeneous_group():
    _n, errors, _w = validate_gpu_group([0, 1])
    assert any("同型号" in e for e in errors)


def test_validate_rejects_non_power_of_two_size():
    _n, errors, _w = validate_gpu_group([0, 1, 2])
    assert any("2 的幂" in e for e in errors)


def test_validate_rejects_dupes_and_single():
    assert validate_gpu_group([0, 0])[1]
    assert validate_gpu_group([0])[1]
    assert validate_gpu_group([0, 9])[1]  # 卡不存在


def test_validate_warns_for_display_cards_not_rejects():
    """审查 #2:组内显示卡与单卡路径一致 —— 只 warning,不拒。"""
    from src.gpu import topology

    with patch.object(topology, "display_gpu_indices", return_value={0}):
        norm, errors, warnings = validate_gpu_group([0, 2])
    assert errors == []
    assert norm == [0, 2]
    assert any("驱动显示服务" in w for w in warnings)


def test_validate_warns_when_group_not_declared_in_hardware_yaml():
    from src.gpu import topology

    other = GPUGroup(id="x", gpus=[4, 5], nvlink=True, role="llm", vram_gb=48)
    with patch.object(topology, "hardware_groups", return_value=[other]):
        _n, errors, warnings = validate_gpu_group([0, 2])
    assert errors == []
    assert any("未在 hardware.yaml 里声明" in w for w in warnings)


def test_validate_skips_probes_when_warnings_disabled():
    """YAML 加载路径(check_warnings=False)不碰 nvidia-smi —— 它可能跑在事件循环上。"""
    from src.gpu import topology

    probe = MagicMock(side_effect=AssertionError("不该探测显示卡"))
    with patch.object(topology, "display_gpu_indices", probe):
        norm, errors, warnings = validate_gpu_group([0, 2], check_warnings=False)
    assert norm == [0, 2] and errors == [] and warnings == []


def test_sanitize_config_gpus_drops_invalid_yaml_group(caplog):
    """审查 #14:YAML 写了异构组 → log error 并忽略该字段,不阻塞启动。"""
    from src.gpu.topology import sanitize_config_gpus

    with caplog.at_level("ERROR"):
        assert sanitize_config_gpus("m", {"gpus": [0, 1]}) is None
    assert "非法" in caplog.text
    assert sanitize_config_gpus("m", {"gpus": [2, 0]}) == [0, 2]
    assert sanitize_config_gpus("m", {"gpus": []}) is None


# ---------------------------------------------------------------------------
# 候选组:权威来源是 hardware.yaml(审查 #1)
# ---------------------------------------------------------------------------

def test_candidate_groups_come_from_hardware_yaml_only():
    groups = candidate_groups()
    assert [g["gpus"] for g in groups] == [[0, 2]]
    assert groups[0]["id"] == "llm-tp"
    assert groups[0]["nvlink"] is True
    assert groups[0]["total_gb"] == pytest.approx(48.0, abs=0.2)


def test_candidate_groups_empty_when_yaml_declares_no_multi_card_group():
    """hardware.yaml 只有单卡 group → 没有候选组。**不会**自己去枚举 [0,2] ——
    那样就绕过了 yaml 里"GPU 0 是显示卡,腾空前别用于 TP"这类运维约束。"""
    from src.gpu import topology

    with patch.object(topology, "hardware_groups", return_value=_SINGLE_GROUPS):
        assert candidate_groups() == []


def test_candidate_groups_drops_heterogeneous_yaml_group(caplog):
    from src.gpu import topology

    bad = GPUGroup(id="bad", gpus=[0, 1], nvlink=False, role="llm", vram_gb=120)
    with patch.object(topology, "hardware_groups", return_value=[bad]), \
            caplog.at_level("ERROR"):
        assert candidate_groups() == []
    assert "不可用于张量并行" in caplog.text


def test_candidate_groups_fills_in_nvlink_from_topo():
    """yaml 没标 nvlink,topo -m 探到直连 → 补缺成 True(topo 只做校验/补缺)。"""
    from src.gpu import topology

    unmarked = GPUGroup(id="g", gpus=[0, 2], nvlink=False, role="llm", vram_gb=48)
    with patch.object(topology, "hardware_groups", return_value=[unmarked]):
        assert candidate_groups()[0]["nvlink"] is True


def test_candidate_groups_flags_display_cards():
    from src.gpu import topology

    with patch.object(topology, "display_gpu_indices", return_value={0}):
        assert candidate_groups()[0]["display_gpus"] == [0]


# ---------------------------------------------------------------------------
# 组预算:组内最小,唯一实现(审查 #4)
# ---------------------------------------------------------------------------

def test_group_budget_takes_min_not_sum():
    stats = _stats({0: 20000, 1: 90000, 2: 12000})
    total, free = group_budget_gb([0, 2], stats)
    assert total == pytest.approx(24.0, abs=0.1)
    assert free == pytest.approx(12000 / 1024, abs=0.1)


def test_group_budget_unknown_cards_returns_zero():
    assert group_budget_gb([7], _stats({0: 1000})) == (0.0, 0.0)
    assert group_budget_gb([]) == (0.0, 0.0)


def test_engines_budget_endpoint_uses_same_source_as_adapter():
    """审查 #4:预算端点与适配器同一个 helper、同一个数据源(nvidia-smi MB)。"""
    from src.api.routes import engines as eng

    stats = _stats({0: 20000, 1: 90000, 2: 12000})
    with patch("src.services.gpu_monitor.poll_gpu_stats", return_value=stats):
        card_total = eng._card_total_gb_for_engine({"gpus": [0, 2]})
        adapter_total, _free = group_budget_gb([0, 2], stats)
    assert card_total == pytest.approx(adapter_total, abs=1e-6)


# ---------------------------------------------------------------------------
# select_tp_group:只在 hardware.yaml 声明过的组里挑
# ---------------------------------------------------------------------------

def test_select_picks_declared_group_that_fits():
    stats = _stats({0: 20000, 1: 10000, 2: 20000})
    assert select_tp_group(30.0, stats=stats) == [0, 2]


def test_select_returns_empty_when_group_cannot_fit():
    stats = _stats({0: 4000, 1: 90000, 2: 4000})
    assert select_tp_group(30.0, stats=stats) == []


def test_select_uses_min_free_not_sum():
    """组内一张卡被占了大半 → 分片装不下那张,整组作废。"""
    stats = _stats({0: 20000, 1: 90000, 2: 4000})
    assert select_tp_group(26.0, stats=stats) == []


def test_select_never_returns_undeclared_combination():
    """哪怕两张 3090 都空着,yaml 没声明这个组就不选(审查 #1)。"""
    from src.gpu import topology

    stats = _stats({0: 24000, 1: 96000, 2: 24000})
    with patch.object(topology, "hardware_groups", return_value=_SINGLE_GROUPS):
        assert select_tp_group(30.0, stats=stats) == []


def test_select_exact_size_filters():
    stats = _stats({0: 24000, 1: 96000, 2: 24000})
    assert select_tp_group(10.0, stats=stats, exact_size=2) == [0, 2]
    assert select_tp_group(10.0, stats=stats, exact_size=4) == []


# ---------------------------------------------------------------------------
# 适配器:只执行 manager 给的放置,绝不存在"不钉卡"分支
# ---------------------------------------------------------------------------

def _dead_popen():
    """假 Popen:已退出、returncode=1。绝不真起 vLLM。"""
    proc = MagicMock()
    proc.poll.return_value = 1
    proc.returncode = 1
    proc.stdout = io.StringIO("boom\n")
    proc.pid = 424242
    return proc


async def _capture_launch(adapter, stats, device=None):
    """真跑一次 _auto_configure + load(),返回 (cmd, env)。子进程是假的 → 必抛 RuntimeError。"""
    captured: dict = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return _dead_popen()

    fake_file = MagicMock()
    fake_file.stat.return_value = MagicMock(st_size=30 * 1024 ** 3)  # 30G 模型

    with patch("src.services.gpu_monitor.poll_gpu_stats", return_value=stats), \
            patch("src.services.inference._placement.estimate_model_size_gb", return_value=30.0), \
            patch.object(adapter._client, "get", new_callable=AsyncMock,
                         side_effect=Exception("no server")), \
            patch("subprocess.Popen", side_effect=_fake_popen), \
            patch.object(adapter, "_kill_process"):
        with pytest.raises(RuntimeError):
            await adapter.load(device)
    return captured["cmd"], captured["env"]


async def test_explicit_group_pins_cuda_visible_devices_and_tp(tmp_path):
    """`gpus=[0, 2]` → env 只看得见这两张卡,且 --tensor-parallel-size 2。"""
    from src.services.inference.llm_vllm import VLLMAdapter

    a = VLLMAdapter(paths={"main": str(tmp_path)}, vllm_port=19999, gpus=[0, 2])
    cmd, env = await _capture_launch(a, _stats({0: 20000, 1: 90000, 2: 20000}), "cuda:0")
    assert env["CUDA_VISIBLE_DEVICES"] == "0,2"
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "2"
    assert a.gpu_indices == [0, 2]


async def test_single_card_is_pinned_too(tmp_path):
    from src.services.inference.llm_vllm import VLLMAdapter

    a = VLLMAdapter(paths={"main": str(tmp_path)}, vllm_port=19999)
    cmd, env = await _capture_launch(a, _stats({0: 20000, 1: 90000, 2: 20000}), "cuda:1")
    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    assert "--tensor-parallel-size" not in cmd
    assert a.gpu_indices == [1]


async def test_tp_without_group_falls_back_to_single_and_still_pins(tmp_path, caplog):
    """审查 #11:params 写了 tp=2 但 manager 没给组 → 退单卡 + 照样钉卡,
    **绝不**"不钉 CUDA_VISIBLE_DEVICES 让子进程看到全部卡"。"""
    from src.services.inference.llm_vllm import VLLMAdapter

    a = VLLMAdapter(paths={"main": str(tmp_path)}, vllm_port=19999, tensor_parallel_size=2)
    with caplog.at_level("ERROR"):
        cmd, env = await _capture_launch(a, _stats({0: 20000, 1: 90000, 2: 4000}), "cuda:0")
    assert env["CUDA_VISIBLE_DEVICES"] == "0"      # 钉住了
    assert "--tensor-parallel-size" not in cmd     # 退到 tp=1
    assert "没有可用的 GPU 组" in caplog.text


async def test_budget_matches_the_card_actually_pinned(tmp_path):
    """审查 #22:预算永远按最终真正钉的那组卡算,不会"按 A 卡算、钉 B 卡"。"""
    from src.services.inference.llm_vllm import VLLMAdapter

    a = VLLMAdapter(paths={"main": str(tmp_path)}, vllm_port=19999)
    stats = _stats({0: 8000, 1: 90000, 2: 8000})   # 30G 模型装不下 cuda:0
    cmd, env = await _capture_launch(a, stats, "cuda:0")
    assert env["CUDA_VISIBLE_DEVICES"] == "0"      # 钉的是 manager 给的 cuda:0
    util = float(cmd[cmd.index("--gpu-memory-utilization") + 1])
    # free 只有 ~7.8G / total 24G → clamp 到 (7.8-0.5)/24 ≈ 0.30,绝不是按 96G 的 Pro 6000 算
    assert util <= (8000 / 1024 - 0.5) / 24 + 0.01


async def test_explicit_tp_narrows_group(tmp_path):
    """显式 tensor_parallel_size 只能收窄组(取前 N 张)。"""
    from src.services.inference.llm_vllm import VLLMAdapter

    a = VLLMAdapter(paths={"main": str(tmp_path)}, vllm_port=19999,
                    gpus=[0, 2], tensor_parallel_size=1)
    cmd, env = await _capture_launch(a, _stats({0: 20000, 1: 90000, 2: 20000}), "cuda:0")
    assert env["CUDA_VISIBLE_DEVICES"] == "0"
    assert "--tensor-parallel-size" not in cmd


async def test_sglang_shares_the_same_placement_rules(tmp_path):
    """审查 #12:SGLang 走同一个 helper —— 组要钉卡 + --tp,不再 `tp = len(gpu_stats)`。"""
    from src.services.inference.llm_sglang import SGLangAdapter

    a = SGLangAdapter(paths={"main": str(tmp_path)}, sglang_port=19998, gpus=[0, 2])
    captured: dict = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return _dead_popen()

    with patch("src.services.gpu_monitor.poll_gpu_stats",
               return_value=_stats({0: 20000, 1: 90000, 2: 20000})), \
            patch("src.services.inference._placement.estimate_model_size_gb", return_value=30.0), \
            patch.object(a._client, "get", new_callable=AsyncMock,
                         side_effect=Exception("no server")), \
            patch("subprocess.Popen", side_effect=_fake_popen), \
            patch.object(a, "_kill_process"):
        with pytest.raises(RuntimeError):
            await a.load("cuda:0")
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "0,2"
    assert captured["cmd"][captured["cmd"].index("--tp") + 1] == "2"


async def test_sglang_single_card_is_pinned(tmp_path):
    from src.services.inference.llm_sglang import SGLangAdapter

    a = SGLangAdapter(paths={"main": str(tmp_path)}, sglang_port=19998)
    captured: dict = {}

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        captured["cmd"] = cmd
        return _dead_popen()

    with patch("src.services.gpu_monitor.poll_gpu_stats",
               return_value=_stats({0: 20000, 1: 90000, 2: 20000})), \
            patch("src.services.inference._placement.estimate_model_size_gb", return_value=4.0), \
            patch.object(a._client, "get", new_callable=AsyncMock,
                         side_effect=Exception("no server")), \
            patch("subprocess.Popen", side_effect=_fake_popen), \
            patch.object(a, "_kill_process"):
        with pytest.raises(RuntimeError):
            await a.load("cuda:2")
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "2"
    assert "--tp" not in captured["cmd"]


# ---------------------------------------------------------------------------
# 放置决策:只在 manager 一处
# ---------------------------------------------------------------------------

def _mgr(stats=None):
    from src.services.gpu_allocator import GPUAllocator
    from src.services.model_manager import ModelManager

    mgr = ModelManager.__new__(ModelManager)
    mgr._allocator = GPUAllocator(poll_fn=lambda: stats or [], hardware_config={"groups": []})
    mgr._models = {}
    return mgr


def _spec(**over):
    from src.services.inference.registry import ModelSpec

    base = dict(id="m", model_type="llm",
                adapter_class="src.services.inference.llm_vllm.VLLMAdapter",
                paths={"main": "m"}, vram_mb=0)
    base.update(over)
    return ModelSpec(**base)


def test_placement_explicit_group_wins_and_reserves_every_card():
    stats = _stats({0: 20000, 1: 90000, 2: 20000})
    mgr = _mgr(stats)
    with patch("src.services.gpu_monitor.poll_gpu_stats", return_value=stats), \
            patch.object(mgr, "_model_size_gb", return_value=30.0):
        pl = mgr._resolve_placement("m", _spec(gpus=[0, 2], vram_mb=30000))
    assert pl.gpu_indices == [0, 2]
    assert pl.reserved_group is not None
    assert set(mgr._allocator._pending) == {0, 2}


def test_placement_explicit_group_on_unsupported_adapter_degrades_to_single(caplog):
    """审查 #15/#21:适配器不支持组 → 按单卡处理 + warning,不做组预留(幻影预留)。"""
    stats = _stats({0: 20000, 1: 90000, 2: 20000})
    mgr = _mgr(stats)
    spec = _spec(adapter_class="src.workers.tts_engines.moss_tts.MOSSTTSEngine",
                 model_type="tts", gpus=[0, 2], vram_mb=8000)
    with caplog.at_level("WARNING"):
        pl = mgr._resolve_placement("m", spec)
    assert pl.gpu_indices == [0]
    assert pl.reserved_group is None
    assert mgr._allocator._pending == {}
    assert "不支持组" in caplog.text


def test_placement_explicit_pin_that_cannot_fit_raises_with_suggestion():
    """审查 #24:显式钉卡是硬约束 —— 放不下就 raise,绝不自动搬到别的卡对。"""
    from src.errors import ModelLoadError

    stats = _stats({0: 4000, 1: 6000, 2: 20000})
    mgr = _mgr(stats)
    with patch("src.services.gpu_monitor.poll_gpu_stats", return_value=stats), \
            patch.object(mgr, "_model_size_gb", return_value=30.0):
        with pytest.raises(ModelLoadError) as ei:
            mgr._resolve_placement("m", _spec(gpu=1))
    assert "硬约束" in str(ei.value)


def test_placement_auto_selects_declared_group_when_single_card_too_small():
    stats = _stats({0: 20000, 1: 10000, 2: 20000})
    mgr = _mgr(stats)
    with patch("src.services.gpu_monitor.poll_gpu_stats", return_value=stats), \
            patch.object(mgr, "_model_size_gb", return_value=30.0):
        pl = mgr._resolve_placement("m", _spec())
    assert pl.gpu_indices == [0, 2]
    assert set(mgr._allocator._pending) == {0, 2}


def test_placement_auto_never_groups_when_yaml_has_none(caplog):
    """yaml 没声明多卡组 → 退单卡 + log error,不自己拼一个异构组。"""
    from src.gpu import topology

    stats = _stats({0: 20000, 1: 10000, 2: 20000})
    mgr = _mgr(stats)
    with patch.object(topology, "hardware_groups", return_value=_SINGLE_GROUPS), \
            patch("src.services.gpu_monitor.poll_gpu_stats", return_value=stats), \
            patch.object(mgr, "_model_size_gb", return_value=30.0), \
            caplog.at_level("ERROR"):
        pl = mgr._resolve_placement("m", _spec())
    assert len(pl.gpu_indices) <= 1
    assert "没有" in caplog.text


def test_placement_honours_requested_tensor_parallel_size():
    stats = _stats({0: 22000, 1: 90000, 2: 22000})
    mgr = _mgr(stats)
    with patch("src.services.gpu_monitor.poll_gpu_stats", return_value=stats), \
            patch.object(mgr, "_model_size_gb", return_value=8.0):
        pl = mgr._resolve_placement("m", _spec(params={"tensor_parallel_size": 2}))
    assert pl.gpu_indices == [0, 2]


def test_instantiate_adapter_injects_group_only_for_supporting_adapters():
    mgr = _mgr()
    adapter = mgr._instantiate_adapter(_spec(gpus=[0, 2]), gpu_group=[0, 2])
    assert adapter._gpus == [0, 2]
    assert mgr._instantiate_adapter(_spec(gpu=1))._gpus is None


# ---------------------------------------------------------------------------
# LoadedModel.cards() —— 「模型占哪些卡」的唯一实现(审查 #5)
# ---------------------------------------------------------------------------

def _entry(gpu_index, gpu_indices, vram_mb=8000, resident=False):
    from src.services.inference.base import InferenceAdapter
    from src.services.model_manager import LoadedModel

    return LoadedModel(spec=_spec(vram_mb=vram_mb, resident=resident),
                       adapter=MagicMock(spec=InferenceAdapter),
                       gpu_index=gpu_index, gpu_indices=gpu_indices)


def test_loaded_model_cards_falls_back_to_primary():
    assert _entry(1, []).cards() == [1]
    assert _entry(0, [0, 2]).cards() == [0, 2]
    assert _entry(-1, []).cards() == []


# ---------------------------------------------------------------------------
# allocator:组预留 / 释放走同一把锁(审查 #3)
# ---------------------------------------------------------------------------

def test_allocator_group_reservation_blocks_concurrent_picks():
    from src.services.gpu_allocator import GPUAllocator

    stats = _stats({0: 20000, 1: 20000, 2: 20000})
    alloc = GPUAllocator(poll_fn=lambda: stats, hardware_config={"groups": []})
    alloc.reserve_gpus([0, 2], 15000)
    assert alloc.get_best_gpu(10000, reserve=False) == 1
    alloc.release_gpus([0, 2], 15000)
    assert alloc._pending == {}


def test_allocator_single_and_group_reservations_share_the_ledger():
    from src.services.gpu_allocator import GPUAllocator

    stats = _stats({0: 20000, 1: 20000, 2: 20000})
    alloc = GPUAllocator(poll_fn=lambda: stats, hardware_config={"groups": []})
    picked = alloc.get_best_gpu(5000)          # 单卡预留
    alloc.reserve_gpus([picked], 5000)          # 组预留叠加到同一张卡
    assert alloc._pending[picked] == 10000
    alloc.release_reservation(picked, 5000)
    alloc.release_gpus([picked], 5000)
    assert alloc._pending == {}


# ---------------------------------------------------------------------------
# vllm_scanner / detector
# ---------------------------------------------------------------------------

def test_scanner_reports_tensor_parallel_size():
    from src.services.inference import vllm_scanner

    ps = (
        "  123 python -m vllm.entrypoints.openai.api_server --model /models/big "
        "--port 8100 --tensor-parallel-size 2\n"
        "  124 python -m vllm.entrypoints.openai.api_server --model /models/small "
        "--port 8101\n"
    )
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps)), \
            patch("httpx.get", side_effect=Exception("down")):
        found = {c["port"]: c for c in vllm_scanner.scan_running_vllm()}
    assert found[8100]["tensor_parallel_size"] == 2
    assert found[8101]["tensor_parallel_size"] == 1


def test_get_device_for_engine_warns_for_every_display_card_in_group(caplog):
    from src.gpu import detector

    fake = [detector.GPUInfo(index=i, name="RTX 3090", vram_total_gb=24.0,
                             compute_capability=(8, 6)) for i in range(3)]
    with patch.object(detector, "get_gpus", return_value=fake), \
            patch.object(detector, "get_display_gpu_indices", return_value={2}):
        with caplog.at_level("WARNING"):
            dev = detector.get_device_for_engine({"gpus": [0, 2]})
    assert dev == "cuda:0"
    assert "GPU group" in caplog.text and "[2]" in caplog.text


def test_get_device_for_engine_group_beats_single_gpu():
    from src.gpu import detector

    fake = [detector.GPUInfo(index=i, name="RTX 3090", vram_total_gb=24.0,
                             compute_capability=(8, 6)) for i in range(3)]
    with patch.object(detector, "get_gpus", return_value=fake), \
            patch.object(detector, "get_display_gpu_indices", return_value=set()):
        assert detector.get_device_for_engine({"gpu": 1, "gpus": [0, 2]}) == "cuda:0"


# ---------------------------------------------------------------------------
# 运行时覆盖三态(审查 #9)
# ---------------------------------------------------------------------------

async def test_override_gpus_round_trip(db_session):
    from src.models.model_runtime_override import ModelRuntimeOverride
    from src.services import runtime_override_store as store

    store.reset_cache()
    await store.set_override(db_session, "my-llm", "gpus", [0, 2])
    assert store.get_overrides()["my-llm"]["gpus"] == [0, 2]
    row = await db_session.get(ModelRuntimeOverride, "my-llm")
    assert row.gpus == [0, 2]
    assert row.gpu == 0  # 主卡同步成组首卡


async def test_single_gpu_override_clears_a_yaml_declared_group(db_session):
    """审查 #9:YAML 配了 gpus 的模型,PATCH 单卡必须真的退出组。

    覆盖里写 `[]`(而不是 NULL)才盖得住 YAML —— NULL 表示"没覆盖过",合并时会回退。
    """
    from src.gpu.topology import resolve_gpus
    from src.services import runtime_override_store as store

    store.reset_cache()
    await store.set_override(db_session, "yaml-grouped", "gpu", 1)
    ov = store.get_overrides()["yaml-grouped"]
    assert ov["gpus"] == []          # 显式清空的哨兵,不是"缺省"
    # 合并:覆盖优先于 YAML 的 gpus:[0,2]
    yaml_entry = {"gpus": [0, 2], "gpu": None}
    merged = {"gpu": ov["gpu"], "gpus": ov["gpus"] if "gpus" in ov else yaml_entry["gpus"]}
    assert resolve_gpus(merged) == [1]


async def test_override_gpus_none_clears(db_session):
    from src.services import runtime_override_store as store

    store.reset_cache()
    await store.set_override(db_session, "my-llm", "gpus", [0, 2])
    await store.set_override(db_session, "my-llm", "gpus", None)
    assert store.get_overrides()["my-llm"]["gpus"] == []


def test_registry_merges_override_clear_over_yaml_group():
    from src.services.inference import registry as reg

    assert reg._ov_gpus({"gpus": []}, {"gpus": [0, 2]}) == []
    assert reg._ov_gpus({}, {"gpus": [0, 2]}) == [0, 2]
    assert reg._ov_gpus({"gpus": [0, 2]}, {}) == [0, 2]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

async def test_gpu_groups_endpoint(db_client):
    resp = await db_client.get("/api/v1/gpu/groups")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert [g["gpus"] for g in groups] == [[0, 2]]
    assert groups[0]["nvlink"] is True


async def test_gpu_groups_endpoint_hints_when_yaml_declares_none(db_client):
    from src.gpu import topology

    with patch.object(topology, "hardware_groups", return_value=_SINGLE_GROUPS):
        resp = await db_client.get("/api/v1/gpu/groups")
    body = resp.json()
    assert body["groups"] == []
    assert "hardware.yaml" in body["hint"]


def _llm_engine_name() -> str:
    """挑一个走 vLLM 适配器(supports_gpu_group)的引擎。"""
    from src.api.routes.engines import _adapter_supports_gpu_group
    from src.config import load_model_configs

    for name, cfg in load_model_configs().items():
        if _adapter_supports_gpu_group(cfg.get("adapter")):
            return name
    pytest.skip("没有走 vLLM/SGLang 适配器的引擎")


def _non_group_engine_name() -> str:
    from src.api.routes.engines import _adapter_supports_gpu_group
    from src.config import load_model_configs

    for name, cfg in load_model_configs().items():
        if not _adapter_supports_gpu_group(cfg.get("adapter")):
            return name
    pytest.skip("没有不支持组的引擎")


async def test_patch_gpu_accepts_group(db_client):
    from src.services import runtime_override_store as store

    name = _llm_engine_name()
    store.reset_cache()
    resp = await db_client.patch(f"/api/v1/engines/{name}/gpu", json={"gpus": [2, 0]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gpus"] == [0, 2]   # 排序归一
    assert body["gpu"] == 0         # 主卡 = 组内第一张
    assert store.get_overrides()[name]["gpus"] == [0, 2]


async def test_patch_gpu_rejects_heterogeneous_group(db_client):
    name = _llm_engine_name()
    resp = await db_client.patch(f"/api/v1/engines/{name}/gpu", json={"gpus": [0, 1]})
    assert resp.status_code == 400
    assert "同型号" in resp.text


async def test_patch_gpu_rejects_group_on_unsupported_engine(db_client):
    """审查 #21:SGLang 之外的引擎(image/tts)配组直接 400,不给幻影预留的机会。"""
    name = _non_group_engine_name()
    resp = await db_client.patch(f"/api/v1/engines/{name}/gpu", json={"gpus": [0, 2]})
    assert resp.status_code == 400
    assert "不支持 GPU 组" in resp.text


async def test_patch_gpu_warns_but_accepts_display_card_group(db_client, caplog):
    """审查 #2:显示卡与单卡路径一致 —— 只 warning,不拒。"""
    from src.gpu import topology

    name = _llm_engine_name()
    with patch.object(topology, "display_gpu_indices", return_value={0}), \
            caplog.at_level("WARNING"):
        resp = await db_client.patch(f"/api/v1/engines/{name}/gpu", json={"gpus": [0, 2]})
    assert resp.status_code == 200
    assert "驱动显示服务" in caplog.text


async def test_patch_gpu_single_card_still_works(db_client):
    """老路径不能坏:`?gpu=1` 查询参数照旧,且把组显式清空。"""
    from src.services import runtime_override_store as store

    name = _llm_engine_name()
    store.reset_cache()
    await db_client.patch(f"/api/v1/engines/{name}/gpu", json={"gpus": [0, 2]})
    resp = await db_client.patch(f"/api/v1/engines/{name}/gpu?gpu=1")
    assert resp.status_code == 200
    assert resp.json() == {"name": name, "gpu": 1, "gpus": None, "applied": False,
                           "hint": "需重新加载模型生效(unload + load)"}
    assert store.get_overrides()[name]["gpus"] == []


def test_engine_info_gpu_is_always_primary_int():
    """审查 #6:`gpu` 永远是主卡 int,`gpus` 是唯一的列表字段。"""
    from src.api.routes.engines import _build_engine_info

    cfg = {"name": "x", "type": "llm", "gpu": 1, "gpus": [0, 2], "vram_gb": 20,
           "adapter": "src.services.inference.llm_vllm.VLLMAdapter"}
    info = _build_engine_info("x", cfg, None, set())
    assert info.gpu == 0
    assert info.gpus == [0, 2]
    assert info.supports_gpu_group is True

    single = _build_engine_info("y", {**cfg, "gpus": None}, None, set())
    assert single.gpu == 1
    assert single.gpus is None
