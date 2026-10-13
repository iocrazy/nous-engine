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
        "vision": True,
    }
