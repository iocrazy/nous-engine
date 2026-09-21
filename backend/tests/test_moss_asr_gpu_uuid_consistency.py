"""回归:MOSS ASR 的 GPU UUID 硬编码在四处,必须永远一致。

2026-09-20 两卡迁移真踩过:两张 3090 拔除、Pro 6000 改由 ComfyUI 独占后,ASR 要从
Pro 6000 搬到 Pro 5000 —— 但四处里**只改了 yaml 一处**,另外三处仍指着 Pro 6000:

  * `infra/moss-asr/start_serve.sh`               ← 手动跑/排障的活路径
  * `infra/systemd/nous-engine-moss-asr.service`  ← 计划里点名的回滚锚点
  * `src/services/inference/asr_sglang.py` 的 `_gpu_uuid` 缺省值
    (它紧邻的注释自己就写着「缺省值必须跟 yaml 一致 —— 否则 yaml 漏写 gpu_uuid 时
     会静默落到别的卡上」,那条不变式当时被它自己违反了)

后果不是「跑不起来」,而是**静默落到 ComfyUI 独占的那张卡上**:抢 ComfyUI 的显存,
而 `mem_fraction_static` 是按 Pro 5000 的 71.7GiB 标定的,在 95.6GiB 卡上含义全变,
manager 那边还把预留记在 GPU 0(幻影预留)。这类错落卡不会报错,只会在出问题时
让人从 nvidia-smi 一路倒查。

`infra/moss-asr/README.md` 当时写的是「硬编码在三处」—— 漏列了 adapter 缺省值那处,
所以连文档都兜不住。这个测试是那条口头约定的可执行版本。

纯文本比对,不起任何子进程、不碰 GPU。
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
YAML_PATH = REPO / "backend" / "configs" / "models.d" / "moss_transcribe_diarize.yaml"
ADAPTER_PATH = REPO / "backend" / "src" / "services" / "inference" / "asr_sglang.py"
START_SH_PATH = REPO / "infra" / "moss-asr" / "start_serve.sh"
UNIT_PATH = REPO / "infra" / "systemd" / "nous-engine-moss-asr.service"

UUID_RE = re.compile(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _yaml_uuid() -> str:
    """生产真相源:models.d 的 params.gpu_uuid。"""
    doc = yaml.safe_load(YAML_PATH.read_text())
    uuid = (doc.get("params") or {}).get("gpu_uuid")
    assert uuid, f"{YAML_PATH} 的 params.gpu_uuid 不该为空 —— 它是四处同步的真相源"
    return uuid


def test_adapter_default_uuid_matches_yaml():
    """adapter 的 _gpu_uuid 缺省值 == yaml。yaml 漏写 gpu_uuid 时就靠它兜底。"""
    src = ADAPTER_PATH.read_text()
    m = re.search(r"_gpu_uuid\s*=\s*gpu_uuid\s+or\s+\"(GPU-[^\"]+)\"", src)
    assert m, f"没在 {ADAPTER_PATH} 里找到 _gpu_uuid 的缺省值字面量(重构了?同步改这个测试)"
    assert m.group(1) == _yaml_uuid(), (
        "adapter 缺省 UUID 与 yaml 不一致 —— yaml 漏写 gpu_uuid 时会静默落到别的卡上"
    )


def test_start_serve_script_uuid_matches_yaml():
    """手动启动脚本 == yaml。排障时跑它,落错卡会抢 ComfyUI 的显存。"""
    found = UUID_RE.findall(START_SH_PATH.read_text())
    assert found, f"没在 {START_SH_PATH} 里找到任何 GPU UUID"
    expected = _yaml_uuid()
    assert all(u == expected for u in found), (
        f"{START_SH_PATH} 里的 UUID {set(found)} 与 yaml 的 {expected} 不一致"
    )


def test_systemd_unit_uuid_matches_yaml():
    """退役 unit == yaml。它是回滚锚点,回滚时落错卡最难查。"""
    found = UUID_RE.findall(UNIT_PATH.read_text())
    assert found, f"没在 {UNIT_PATH} 里找到任何 GPU UUID"
    expected = _yaml_uuid()
    assert all(u == expected for u in found), (
        f"{UNIT_PATH} 里的 UUID {set(found)} 与 yaml 的 {expected} 不一致"
    )


def test_asr_never_pinned_to_the_comfyui_card():
    """ASR 的四处都不能是 Pro 6000 —— 那张卡 2026-09-20 起由 ComfyUI 独占。

    这条与上面三条是不同的防线:上面防「四处彼此分叉」,这条防「四处一起改错成
    ComfyUI 那张卡」。ComfyUI 自己的 unit 用这个 UUID 是对的,所以只查 ASR 这四个文件。
    """
    comfy_uuid = "GPU-d24ed424-5712-55e9-9b95-77d997ac80dc"
    for path in (YAML_PATH, ADAPTER_PATH, START_SH_PATH, UNIT_PATH):
        text = path.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            # 注释里提到这个 UUID 是允许的(「绝不能落那张」之类的说明)
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            assert comfy_uuid not in line, (
                f"{path} 把 ASR 钉到了 ComfyUI 独占的 Pro 6000 上:{stripped!r}"
            )
