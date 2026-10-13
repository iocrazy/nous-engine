import json
import subprocess
from pathlib import Path

import yaml


_REPO = Path(__file__).resolve().parents[2]
_HELPER = _REPO / "infra/systemd/strata-control"
_UNIT = _REPO / "infra/systemd/nous-engine-strata@.service"
_INSTALL = _REPO / "infra/systemd/install.sh"
_SUDOERS = _REPO / "infra/security/nous-engine-strata.sudoers"
_MODEL = (
    _REPO
    / "backend/configs/models.d/qwen3_8_flash_next_abliterated_iq3s_strata.yaml"
)
_IQ4_MODEL = (
    _REPO
    / "backend/configs/models.d/qwen3_8_flash_next_uncensored_iq4xs_strata.yaml"
)
_STRATA_CONFIGS = _REPO / "infra/strata"


def test_control_helper_rejects_unsafe_instance_before_systemctl():
    result = subprocess.run(
        ["bash", str(_HELPER), "start", "../../backend"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert "invalid Strata instance" in result.stderr


def test_control_helper_only_accepts_start_and_stop():
    result = subprocess.run(
        ["bash", str(_HELPER), "restart", "iq3s"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert "usage:" in result.stderr


def test_control_helper_rejects_extra_arguments():
    result = subprocess.run(
        ["bash", str(_HELPER), "start", "iq3s", "another-unit"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert "usage:" in result.stderr


def test_strata_unit_is_isolated_and_not_enabled_by_installer():
    unit = _UNIT.read_text(encoding="utf-8")
    install = _INSTALL.read_text(encoding="utf-8")

    assert "User=heygo" in unit
    assert "EnvironmentFile=/etc/nous-engine/strata/%i.env" in unit
    assert "--host 127.0.0.1" in unit
    assert "--host 0.0.0.0" not in unit
    assert "KillMode=control-group" in unit
    assert "MemoryMax=72G" in unit
    assert "WorkingDirectory=/media/heygo/program/projects-code/github-repos/Strata" in unit
    assert "nous-engine-strata@.service" in install
    services_line = next(line for line in install.splitlines() if line.startswith("SERVICES="))
    assert "strata" not in services_line


def test_installer_copies_control_helper_root_owned():
    install = _INSTALL.read_text(encoding="utf-8")
    lines = [
        line for line in install.splitlines()
        if "install " in line and "strata-control" in line
    ]
    assert len(lines) == 1
    assert "-o root -g root" in lines[0]
    assert "$PRIV_LIBDIR/strata-control" in lines[0]


def test_sudoers_only_exposes_argument_validating_helper():
    sudoers = _SUDOERS.read_text(encoding="utf-8").strip()
    assert sudoers == (
        "heygo ALL=(root) NOPASSWD: /usr/local/lib/nous-engine/strata-control *"
    )
    assert "systemctl" not in sudoers


def test_qwen38_iq3s_model_uses_managed_strata_on_gpu1():
    cfg = yaml.safe_load(_MODEL.read_text(encoding="utf-8"))

    assert cfg["adapter"] == "src.services.inference.llm_strata.ManagedStrataAdapter"
    assert cfg["paths"]["main"] == "llm/qwen3.8-flash-next"
    assert cfg["resident"] is False
    assert cfg["gpu"] == 1
    assert cfg["vram_mb"] == 90000
    assert cfg["params"] == {
        "strata_base_url": "http://127.0.0.1:18080",
        "strata_instance": "qwen38-iq3s",
        "max_model_len": 8192,
        "strata_context_presets": {
            8192: "qwen38-iq3s",
            262144: "qwen38-iq3s-256k",
            524288: "qwen38-iq3s-512k",
        },
        "vision": True,
    }


def test_qwen38_context_configs_have_matching_engine_flags():
    expected = {
        "qwen38-iq3s-8k.json": (8192, []),
        "qwen38-iq3s-256k.json": (262144, ["--kv", "int8"]),
        "qwen38-iq3s-512k.json": (
            524288,
            ["--kv", "int8", "--rope-scaling", "yarn", "--rope-scale", "2"],
        ),
    }
    for filename, (context, required) in expected.items():
        cfg = json.loads((_STRATA_CONFIGS / filename).read_text(encoding="utf-8"))
        args = cfg["args"]
        assert args[args.index("--max-context") + 1] == str(context)
        assert cfg["port"] == 18080
        assert cfg["gpu"] == 1
        assert "--vision" in args
        for index in range(0, len(required), 2):
            flag, value = required[index:index + 2]
            assert args[args.index(flag) + 1] == value


def test_qwen38_iq4xs_model_is_separate_managed_strata_engine():
    cfg = yaml.safe_load(_IQ4_MODEL.read_text(encoding="utf-8"))

    assert cfg["id"] == "qwen3_8_flash_next_uncensored_iq4xs_strata"
    assert cfg["adapter"] == "src.services.inference.llm_strata.ManagedStrataAdapter"
    assert cfg["paths"]["main"] == "llm/qwen3.8-flash-next/Uncensored-IQ4_XS"
    assert cfg["resident"] is False
    assert cfg["gpu"] == 1
    assert cfg["vram_mb"] == 90000
    assert cfg["params"]["strata_instance"] == "qwen38-iq4xs"
    assert cfg["params"]["strata_context_presets"] == {
        8192: "qwen38-iq4xs",
        262144: "qwen38-iq4xs-256k",
        524288: "qwen38-iq4xs-512k",
    }


def test_qwen38_iq4xs_context_configs_use_uncensored_pack_and_mmproj():
    expected = {
        "qwen38-iq4xs-8k.json": (8192, False),
        "qwen38-iq4xs-256k.json": (262144, False),
        "qwen38-iq4xs-512k.json": (524288, True),
    }
    for filename, (context, yarn) in expected.items():
        cfg = json.loads((_STRATA_CONFIGS / filename).read_text(encoding="utf-8"))
        args = cfg["args"]
        assert args[args.index("--max-context") + 1] == str(context)
        assert args[args.index("--pack") + 1].endswith("/packs/orca-iq4_xs")
        assert args[args.index("--native") + 1].endswith(
            "/Qwen3.8-Flash-Next-Uncensored.IQ4_XS.gguf"
        )
        assert args[args.index("--ple-io") + 1] == "mmap"
        assert cfg["vision"]["mmproj"].endswith(
            "/Qwen3.8-Flash-Next-Uncensored.mmproj-f16.gguf"
        )
        assert ("--rope-scaling" in args) is yarn
