"""spec 2026-09-05 §10:所有 resident: true 的模型按落卡汇总显存,必须放得进 hardware.yaml
的容量减 DEFAULT_RESERVED_GB。常驻集合不自洽在合 PR 前就红,不等上线。

Task 1-3 之后数据面不再懒加载(未加载即刻 503 model_not_ready),「该常驻的必须真常驻」
从运维习惯变成硬约束 —— 所以常驻集合装不装得下,得由测试而不是启动期日志来兜。

不碰 nvidia-smi、不碰 GPU:容量来自 hardware.yaml 的静态声明,占用来自 yaml 的显存声明。
"""
from __future__ import annotations

from collections import defaultdict

from src.config import load_hardware_config, load_model_configs
from src.gpu.topology import resolve_gpus
from src.services.gpu_monitor import DEFAULT_RESERVED_GB
from src.services.model_scanner import scan_models


def _capacity_gb_by_gpu() -> dict[int, float]:
    """每张卡的容量(GB)。

    单卡 group 的 vram_gb 就是该卡容量,权威。多卡 group(llm-tp = [0, 2],vram_gb 48)
    只用来**补缺**:GPU 0 是显示卡,hardware.yaml 里没给它单卡 group,只在 llm-tp 组里
    出现过 —— 不补的话 3.8 常驻在 [0, 2] 会被误判成「GPU 0 不在 hardware.yaml 里」。
    组内同型号是 validate_gpu_group 的硬性校验,所以按卡数均分得到的就是单卡容量。
    """
    cap: dict[int, float] = {}
    multi: dict[int, float] = {}
    for g in load_hardware_config().get("groups", []):
        gpus = list(g.get("gpus") or [])
        if not gpus:
            continue
        per_card = float(g["vram_gb"]) / len(gpus)
        if len(gpus) == 1:
            cap[gpus[0]] = per_card
        else:
            for gid in gpus:
                multi.setdefault(gid, per_card)
    for gid, v in multi.items():
        cap.setdefault(gid, v)
    return cap


def test_resident_models_are_pinned_and_fit():
    cap = _capacity_gb_by_gpu()
    assert cap, "hardware.yaml 没有 group,无法推容量"
    used: dict[int, float] = defaultdict(float)
    for key, cfg in scan_models().items():
        if not cfg.get("resident"):
            continue
        gpus = resolve_gpus(cfg)
        assert gpus, f"常驻模型 {key} 必须显式钉卡(gpu/gpus),常驻不能靠自动选卡"
        # scan_models 把 yaml 的 vram_mb 归一成 vram_gb;读错键名会让 share 恒为 0,
        # 整个测试变成空真(断言 0 <= 容量),这个 assert 就是防这个的。
        vram_gb = float(cfg.get("vram_gb") or 0)
        assert vram_gb > 0, f"常驻模型 {key} 没有显存声明(vram_mb),容量核算无从谈起"
        # tp 组按卡均分 —— 镜像的是 model_manager.py 的预留口径
        # (`per_card = max(1, int(spec.vram_mb / len(explicit)))`),不是
        # topology.group_budget_gb(那条算的是「组内最小卡的可用量」,另一回事)。
        share = vram_gb / len(gpus)
        for g in gpus:
            used[g] += share
    assert used, "没有扫到任何常驻模型 —— 目录不该是空的,测试会失去意义"
    for g, total in sorted(used.items()):
        assert g in cap, f"GPU {g} 不在 hardware.yaml 里"
        assert total <= cap[g] - DEFAULT_RESERVED_GB, (
            f"GPU {g} 常驻合计 {total:.1f}G 超过 {cap[g]}G - 预留 {DEFAULT_RESERVED_GB}G"
        )


def test_qwen3_6_is_retired():
    """spec §10:3.6 退役,目录唯一 LLM 是 3.8。

    断言的是**目录**(configs/models.d/*.yaml),不是 scan_models():权重目录
    `llm/Qwen3.6-35B-A3B-FP8` 按设计留盘不动,而 scan_models 会把任何带 config.json 的
    目录自动探测成 llm(auto_detected,resident 恒 False),所以它那边这个 key 永远还在。
    退役的含义是「不再由目录声明、不再可常驻/被服务引用」,这正是 load_model_configs 的口径。
    """
    catalog = load_model_configs(apply_overrides=False)
    assert "qwen3_6_35b_a3b_fp8" not in catalog, "spec §10:3.6 退役,目录唯一 LLM 是 3.8"
    llms = sorted(k for k, v in catalog.items() if v.get("type") == "llm")
    assert llms == ["qwen3_8_27b_abliterated_awq"], f"目录唯一 LLM 应是 3.8,实际 {llms}"
