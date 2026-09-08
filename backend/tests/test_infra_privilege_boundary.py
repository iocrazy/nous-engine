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


#: `--listen` 接受逗号分隔的地址表(main.py 对它 split(",") 后每个地址各起一个
#: TCPSite),所以判定必须**逐个**看,不能对整条 ExecStart 做子串匹配。
_WILDCARD_BINDS = {"0.0.0.0", "::", "*", ""}


def _comfy_listen_addrs() -> list[str]:
    import re

    exec_start = _exec_start(_read("infra/systemd/nous-engine-comfyui.service"))
    m = re.search(r"--listen[= ]+(\S+)", exec_start)
    assert m, "ComfyUI 必须显式 --listen,不能靠默认值(默认就是 127.0.0.1,但别赌):" + exec_start
    return [a.strip() for a in m.group(1).split(",")]


def test_comfyui_binds_no_wildcard_address():
    """零鉴权 + 节点可执行任意代码 → 每个绑定地址都必须是具体网卡,绝不能是通配符。

    #725 的原判定是 `"--listen 0.0.0.0" not in exec_start`,#727 把参数改成逗号表
    (`127.0.0.1,10.0.0.10`)之后就有了盲区:`--listen 127.0.0.1,0.0.0.0` 同样能过 ——
    子串里 `--listen 0.0.0.0` 并不出现。逐个地址判定才咬得住。
    """
    addrs = _comfy_listen_addrs()
    bad = [a for a in addrs if a in _WILDCARD_BINDS]
    assert not bad, (
        f"ComfyUI 零鉴权且节点可执行任意代码,绑定里不能有通配地址 {bad}(当前 {addrs});"
        "要逐个点名网卡 —— 回环给后端桥,另一个给 ZeroTier。"
    )


def test_comfyui_keeps_loopback_for_the_bridge():
    """后端桥走 NOUS_COMFY_URL(默认回环),所以回环这一项不能被摘掉。"""
    addrs = _comfy_listen_addrs()
    assert any(a.startswith("127.") or a == "localhost" for a in addrs), (
        f"绑定里没有回环({addrs}):后端桥按 NOUS_COMFY_URL 连 127.0.0.1 会 Connection refused"
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
