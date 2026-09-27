"""ExternalCliProvider ABC + 契约模型。

provider 只负责「驱动 CLI → 产出本地产物文件」,**不碰 nous-engine 的存储/签名 URL**
(那是 inline 节点的事,保持子系统与存储层解耦)。所有 provider 共享 ExternalGenRequest /
ExternalGenResult 契约,以便 governor 和节点对所有 provider 一视同仁。
"""
from __future__ import annotations

import asyncio
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Sequence

from pydantic import BaseModel, Field


class ProviderStatus(BaseModel):
    """provider 探活结果(装没装 / 登没登 / 额度)。"""

    name: str
    available: bool = False          # CLI 可执行且基本可用
    logged_in: bool = False          # 账号已登录
    version: str = ""
    quota: str | None = None         # 账号额度文本(如即梦 user_credit)
    modalities: list[str] = Field(default_factory=list)
    message: str = ""


class ArtifactRef(BaseModel):
    """provider 产出的一份产物。local_path = 本机绝对路径(节点负责转签名 URL)。"""

    kind: str                        # image | video | audio
    local_path: str
    title: str = ""


class ExternalGenRequest(BaseModel):
    """统一生成请求(所有 provider 共用,各自映射到自家 CLI 参数)。"""

    prompt: str
    negative_prompt: str = ""
    width: int = Field(1024, ge=64, le=8192)
    height: int = Field(1024, ge=64, le=8192)
    num_images: int = Field(1, ge=1, le=8)
    # 参考图:本机绝对路径(节点已把签名 URL / base64 解析成本地文件再传入)。
    input_images: list[str] = Field(default_factory=list)
    model: str | None = None         # provider 自家模型标识(如即梦 "4.0")
    timeout_s: float | None = None
    extra: dict = Field(default_factory=dict)


class ExternalGenResult(BaseModel):
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    text: str = ""                   # 部分 provider(codex)会附文本回复
    elapsed_ms: int = 0


class ProviderError(RuntimeError):
    """provider 调用失败(CLI 不可用 / 退出码非 0 / 没产出媒体等)。

    携带 user-facing 中文 message + 可选 status_code(供路由层映射 HTTP)。
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ExternalCliProvider(ABC):
    """账号登录态外部 CLI 的统一适配面。

    子类实现 probe_status / login_start / generate。executable 解析与 subprocess
    调用的公共逻辑放在本基类(_resolve_executable / _run),子类只拼自家 args + 解析输出。
    """

    name: str = ""
    modalities: set[str] = set()      # {"image", "video", ...}
    # 默认 CLI 命令(子类覆盖,如 "dreamina" / "codex");配置里 executable 优先。
    default_executable: str = ""

    def __init__(self, *, executable: str = "", cwd: str | None = None) -> None:
        self._executable = (executable or self.default_executable or self.name).strip()
        self._cwd = cwd

    # ---- executable 解析 -------------------------------------------------

    def resolve_executable(self) -> str:
        """解析可执行文件:配置的绝对路径优先,否则 PATH 查找,失败返回原始名。"""
        raw = self._executable or self.name
        if "/" in raw or "\\" in raw:
            return raw
        return shutil.which(raw) or raw

    def is_installed(self) -> bool:
        exe = self.resolve_executable()
        return bool(exe) and (("/" in exe or "\\" in exe) or shutil.which(exe) is not None)

    # ---- subprocess 公共封装 --------------------------------------------

    async def _run(
        self,
        args: Sequence[str],
        *,
        timeout: float = 120.0,
        cwd: str | None = None,
        stdin_data: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """跑一次 CLI,返回 (returncode, stdout, stderr)。超时 kill 并返回 124。

        stdin_data:喂给子进程 stdin 的文本(如 codex 的 prompt 走 `-`)。
        env:额外环境变量(并入 os.environ,如 codex 的 workspace 目录)。
        失败抛 ProviderError(executable 找不到 / 超时)由调用方决定如何转译。
        """
        exe = self.resolve_executable()
        if not self.is_installed():
            raise ProviderError(
                f"{self.name} CLI 不可用:未找到可执行文件 {exe!r}。请确认已安装并登录。",
                status_code=400,
            )
        clean = [str(a) for a in args if str(a) != ""]
        run_env = {**os.environ, **env} if env else None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                *clean,
                cwd=cwd or self._cwd,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=run_env,
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"{self.name} CLI 未找到:{exe}", status_code=400) from exc
        stdin_bytes = stdin_data.encode("utf-8") if stdin_data is not None else None
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes), timeout=timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise ProviderError(
                f"{self.name} CLI 执行超时({timeout:.0f}s):{' '.join(clean[:2])}",
                status_code=504,
            ) from exc
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        return (proc.returncode if proc.returncode is not None else 124, stdout, stderr)

    # ---- 子类必须实现 ----------------------------------------------------

    @abstractmethod
    async def probe_status(self) -> ProviderStatus: ...

    @abstractmethod
    async def login_start(self) -> dict:
        """触发 CLI 自身的登录流程,返回引导信息(如二维码 / OAuth 链接)。"""
        ...

    @abstractmethod
    async def generate(self, req: ExternalGenRequest) -> ExternalGenResult: ...
