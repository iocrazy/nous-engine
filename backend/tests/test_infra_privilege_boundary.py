"""特权边界的静态守卫(2026-09-07 安全审查)。

这台机器上「拿到 root」的路径必须是**刻意的特权动作**(有人 sudo 跑 install.sh),
而不是「往仓库里写个文件、等下次重启」。本文件把三条属性锁进 CI:

1. root 单元 `nous-engine-netprobe` 执行的脚本不在用户可写的仓库里,而在
   root 属主的 `/usr/local/lib/nous-engine/`。否则链条是:
   任何进到 master 的提交 → deploy.sh 的 `git reset --hard` → 下次单元启动 =
   免密 root 代码执行(仓库是 public、master 可直推,放大器齐全)。
2. ComfyUI 不监听 0.0.0.0 —— 它自身零鉴权,而节点能读写任意文件、执行代码,
   暴露到局域网/ZeroTier 等于把这台机器交出去。后端桥走 127.0.0.1,绑回环不影响它。
3. deploy.sh 在 `git reset --hard` 前必须先挡住脏工作树 —— 静默销毁与本仓库
   别处的 fail-loud 风格(dist 时间戳校验、`import vllm` 校验)不一致。

都是文本断言:CI 里没有 systemd,真机行为只能靠这层 + 真机验收。
"""

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_UNITS = _REPO / "infra/systemd"

#: 采集器安装到的 root 属主目录(install.sh 与 unit 必须一致)。
PRIVILEGED_LIBDIR = "/usr/local/lib/nous-engine"


def _read(rel: str) -> str:
    p = _REPO / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


def _privileged_libdir_refs(line: str) -> bool:
    """install.sh 里用的是 shell 变量 $PRIV_LIBDIR;字面量与变量两种写法都算引用它。"""
    return PRIVILEGED_LIBDIR in line or "$PRIV_LIBDIR" in line or "${PRIV_LIBDIR}" in line


def _exec_start(unit_text: str) -> str:
    lines = [ln for ln in unit_text.splitlines() if ln.startswith("ExecStart=")]
    assert len(lines) == 1, f"期望恰好一条 ExecStart=,实际 {len(lines)} 条"
    return lines[0]


# ── 1. root 单元不得执行仓库里的文件 ────────────────────────────────────────


def test_netprobe_unit_runs_as_root():
    """前提确认:它确实是 root 单元 —— 下面两条断言才有意义。"""
    assert "User=root" in _read("infra/systemd/nous-engine-netprobe.service")


def test_netprobe_execstart_is_outside_the_repo():
    exec_start = _exec_start(_read("infra/systemd/nous-engine-netprobe.service"))
    assert PRIVILEGED_LIBDIR in exec_start, (
        f"root 单元的 ExecStart 必须指向 {PRIVILEGED_LIBDIR}(root 属主),"
        f"当前:{exec_start}"
    )
    for repo_marker in ("/repos/nous-engine", "infra/monitoring"):
        assert repo_marker not in exec_start, (
            f"root 单元的 ExecStart 指到了用户可写的仓库路径({repo_marker}):{exec_start}"
        )


def test_install_sh_defines_the_privileged_libdir():
    """变量得真的指向那个 root 属主目录 —— 下面几条才有意义。"""
    install_sh = _read("infra/systemd/install.sh")
    assert f"PRIV_LIBDIR={PRIVILEGED_LIBDIR}\n" in install_sh, (
        f"install.sh 应定义 PRIV_LIBDIR={PRIVILEGED_LIBDIR}"
    )


def test_install_sh_copies_collector_root_owned():
    """install.sh(以 root 跑)负责把采集器复制到 root 属主目录。"""
    install_sh = _read("infra/systemd/install.sh")
    for fname in ("nous-netprobe.py", "netprobe.bt"):
        copied = [
            ln
            for ln in install_sh.splitlines()
            if "install " in ln and fname in ln and _privileged_libdir_refs(ln)
        ]
        assert copied, f"install.sh 没把 {fname} 装进 {PRIVILEGED_LIBDIR}"
        for ln in copied:
            assert "-o root -g root" in ln, f"{fname} 的安装没指定 root 属主:{ln.strip()}"


def test_install_sh_dry_runs_the_installed_copy():
    """自检要跑**装好的那份**;跑仓库那份等于验了一个之后不会被执行的文件。"""
    install_sh = _read("infra/systemd/install.sh")
    dry_run_lines = [
        ln
        for ln in install_sh.splitlines()
        if "--dry-run" in ln and not ln.lstrip().startswith("#")  # 注释里提一嘴不算
    ]
    assert dry_run_lines, "install.sh 缺 bpftrace --dry-run 自检"
    for ln in dry_run_lines:
        assert _privileged_libdir_refs(ln), f"--dry-run 验的不是装好的那份:{ln.strip()}"


def test_uninstall_removes_the_privileged_libdir():
    install_sh = _read("infra/systemd/install.sh")
    tail = install_sh.split("uninstall)")[-1]
    assert any(_privileged_libdir_refs(ln) and "rm " in ln for ln in tail.splitlines()), (
        f"uninstall 分支没清理 {PRIVILEGED_LIBDIR}"
    )


# ── 2. ComfyUI 不得暴露到局域网 ─────────────────────────────────────────────


def _comfy_listen_addr() -> str:
    import re

    exec_start = _exec_start(_read("infra/systemd/nous-engine-comfyui.service"))
    m = re.search(r"--listen\s+(\S+)", exec_start)
    assert m, "ComfyUI 必须显式 --listen 一个地址,不能靠默认值:" + exec_start
    return m.group(1)


def test_comfyui_does_not_listen_on_all_interfaces():
    """零鉴权 + 节点可执行任意代码 → 只能绑到一个具体地址,绝不能是 0.0.0.0。"""
    addr = _comfy_listen_addr()
    assert addr not in ("0.0.0.0", "::", "*"), (
        f"ComfyUI 零鉴权且节点可执行任意代码,不能监听所有网卡(当前 {addr});"
        "绑回环走 SSH 隧道,或绑到某张受控网卡(如 ZeroTier)的具体地址。"
    )


def test_comfyui_non_loopback_bind_waits_for_its_interface():
    """绑非回环地址时,必须等那张网卡起来,否则开机自启会 bind 失败。

    2026-09-07:用户把 comfyui 设成开机自启并要绑 ZeroTier 地址 10.0.0.10。该地址在
    ztu7tc2vml 上,由 zerotier-one.service 异步分配 —— 只有 `After=network.target`
    时 comfy 很可能早于它启动,bind 到不存在的地址直接失败。所以非回环绑定必须同时:
    ① 排在 zerotier-one 之后;② `StartLimitIntervalSec=0`,让 Restart=on-failure 的
    重试不会在开机窗口内被启动次数上限掐死(与 backend/status 两个单元同样的写法)。
    """
    addr = _comfy_listen_addr()
    if addr.startswith("127.") or addr == "localhost":
        return  # 绑回环没有网卡时序问题
    unit = _read("infra/systemd/nous-engine-comfyui.service")
    assert "zerotier-one.service" in unit, (
        f"comfyui 绑非回环地址 {addr},但没有声明对 zerotier-one.service 的顺序依赖 —— "
        "开机时地址还不存在,bind 会失败"
    )
    assert "After=" in unit and "zerotier-one.service" in unit.split("[Service]")[0], (
        "对 zerotier-one 的依赖要写在 [Unit] 段的 After=/Wants= 里"
    )
    assert "StartLimitIntervalSec=0" in unit, (
        "绑具体网卡地址时必须 StartLimitIntervalSec=0,否则开机重试会被启动次数上限掐死"
    )


# ── 3. deploy.sh 不得静默销毁 ───────────────────────────────────────────────


def test_deploy_guards_dirty_tree_before_reset_hard():
    deploy = _read("infra/deploy.sh")
    idx_reset = deploy.find("git reset --hard")
    assert idx_reset != -1, "deploy.sh 里找不到 git reset --hard"
    before = deploy[:idx_reset]
    assert "status --porcelain" in before, (
        "`git reset --hard` 之前必须先检查工作树是否干净(git status --porcelain),"
        "否则会静默丢弃生产机上的本地改动"
    )
    assert "--force" in deploy, "应提供显式 --force 才允许覆盖脏树"


@pytest.mark.parametrize("unit", ["nous-engine-netprobe.service"])
def test_privileged_units_keep_filesystem_protections(unit: str):
    """root 单元的文件系统收紧不能在改 ExecStart 时被顺手删掉。"""
    text = _read(f"infra/systemd/{unit}")
    for directive in ("ProtectSystem=strict", "ProtectHome=yes", "ReadWritePaths=/run/nous-engine"):
        assert directive in text, f"{unit} 缺 {directive}"
