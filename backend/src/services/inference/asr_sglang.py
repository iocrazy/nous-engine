"""SGLangOmniAdapter — 把 MOSS-Transcribe-Diarize 的 sgl-omni serve 微服务纳入
ModelManager 统一生命周期(spec 2026-07-20-moss-asr-sglang-serving §7 Arc 2)。

契约与 `llm_vllm.VLLMAdapter` 同款(ModelManager 期望的 load/unload/health/base_url/
_port/pid/is_loaded),但**不是** VLLMAdapter 的克隆,关键差异:

1. 子进程不是 `python -m vllm...`,而是 `<venv>/bin/python <venv>/bin/sgl-omni serve
   --config moss_config.yaml`(独立 venv:sglang-omni 自带 torch 2.11/transformers 5.6,
   与 backend 的 vllm 0.22 钉两条互不相干的升级轨,故独立 venv/工具链)。
2. **GPU 钉卡不用 device index,用 UUID**。VLLMAdapter 把 `CUDA_VISIBLE_DEVICES` 设成
   ModelManager 传进来的 `device` 里的 index;这里**故意忽略 device index**,改用
   yaml `params.gpu_uuid` 钉卡 —— 因为 sgl-omni 子进程内 torch 默认 FASTEST_FIRST
   枚举不跟随 PCI 序,裸 index 会落到哪张卡不可预期。UUID 不随槽位/枚举顺序变。配合
   `CUDA_DEVICE_ORDER=PCI_BUS_ID`,进程内只见这一张卡 = cuda:0(moss_config.yaml 里
   device=cuda:0 即指它)。
   生产钉的是 **Pro 6000**(2026-09-05 #709 从 3090 搬来,两张 3090 整块让给 Qwen3.8
   做 tp=2)。老注释写的「绝不 Pro 6000」(GSP 固件负载崩卡)**已失效** —— 该固件问题
   2026-08-11 经用户确认解决,驱动升到 595.91.07 后零 Xid,ComfyUI 长期在这张卡上满载
   出图出片。infra/gpu/README.md 留档仅供复发时排查。
3. env 完整复刻 `infra/moss-asr/start_serve.sh`(CUDA_HOME 指 venv 内 cu13 工具链、
   PATH 前插 venv/bin + cu13/bin、LD_LIBRARY_PATH、HF 离线、NO_PROXY 回环)。
4. unload 走仓库既有 `safe_signal.safe_killpg` 杀**整进程组**(start_new_session=True
   建独立组)。sglang worker 子进程 cmdline 不含引擎名,只杀主进程会留孤儿囤显存
   (SPIKE.md 实测累计 22GB);killpg 收整组根治。safe_killpg 拒 pgid<=1(广播防护,
   killpg 广播事故教训)。
5. 转写不走 `adapter.infer`:端点直连 `base_url` 的 `/v1/audio/transcriptions`
   HTTP(同 chat/embeddings 直连 vLLM base_url 的做法),故 infer 未实现。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx

from src.services.inference.base import (
    InferenceAdapter,
    InferenceRequest,
    InferenceResult,
    MediaModality,
)

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    """仓库根,从本文件位置推导(不硬编码用户名/绝对路径)。
    backend/src/services/inference/asr_sglang.py → parents[4] = 仓库根。"""
    return Path(__file__).resolve().parents[4]


class SGLangOmniAdapter(InferenceAdapter):
    """spawn `sgl-omni serve` 子进程管 MOSS-Transcribe-Diarize,纳入 ModelManager。

    load()   → 起 sgl-omni 子进程(独立进程组)→ 等 /health 200
    unload() → safe_killpg 杀整进程组(SIGTERM→等退出→超时 SIGKILL)→ 释放显存
    health   → GET /health == 200
    infer()  → 未实现(ASR 经端点直连 base_url 的 /v1/audio/transcriptions)
    """

    modality = MediaModality.AUDIO
    # mem_fraction_static 0.15 × 96GB(Pro 6000)≈14.4GB;供 UI/预算展示(落卡钉 gpu:1)。
    # 该比例按整卡容量算,换卡要同步改 moss_config.yaml,否则这里的预算账也跟着失真。
    estimated_vram_mb = 15000

    def __init__(
        self,
        paths: dict[str, str],
        device: str = "cuda",
        *,
        gpu_uuid: str | None = None,
        asr_port: int | None = None,
        config_path: str | None = None,
        venv_dir: str | None = None,
        health_path: str = "/health",
        startup_timeout: int = 600,
        kill_timeout: int = 15,
        **kwargs: Any,
    ):
        super().__init__(paths=paths, device=device)
        # 路径从仓库根推导,params 可覆盖(测试/临时实例)。
        root = _repo_root()
        self._venv_dir = Path(venv_dir) if venv_dir else root / "infra" / "moss-asr" / ".venv"
        self._config_path = (
            Path(config_path) if config_path
            else root / "infra" / "moss-asr" / "moss_config.yaml"
        )
        # GPU 钉卡:UUID。缺省 = 生产那张 Pro 6000(与 moss_transcribe_diarize.yaml 的
        # params.gpu_uuid 同一张;2026-09-05 #709 从 3090 搬来,两张 3090 让给 LLM 做 tp=2)。
        # 缺省值必须跟 yaml 一致 —— 否则 yaml 漏写 gpu_uuid 时会静默落到别的卡上。
        self._gpu_uuid = gpu_uuid or "GPU-d24ed424-5712-55e9-9b95-77d997ac80dc"
        self._port = asr_port or 0
        self._health_path = "/" + health_path.lstrip("/")
        self._startup_timeout = startup_timeout
        self._kill_timeout = kill_timeout

        self._base_url: str | None = (
            f"http://127.0.0.1:{self._port}" if self._port else None
        )
        self.base_url = self._base_url
        self._process: subprocess.Popen | None = None
        self._managed = True
        # trust_env=False:只连本地子进程,彻底绕开本机 mihomo 代理(env proxy 请求期解析,
        # proxy=None 不够;与 vLLM/SGLang adapter 取齐)。
        self._client = httpx.AsyncClient(
            timeout=30, limits=httpx.Limits(max_connections=10), trust_env=False
        )
        # 后台抽干 stdout 进有界 deque,防 PIPE(~64KB)填满阻塞子进程(同 vLLM adapter)。
        self._stdout_tail: deque[str] = deque(maxlen=200)
        self._drain_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 子进程构建
    # ------------------------------------------------------------------

    def _build_argv(self, port: int) -> list[str]:
        py = str(self._venv_dir / "bin" / "python")
        sgl = str(self._venv_dir / "bin" / "sgl-omni")
        return [
            py, sgl, "serve",
            "--config", str(self._config_path),
            "--host", "127.0.0.1",
            "--port", str(port),
        ]

    def _build_env(self) -> dict[str, str]:
        """完整复刻 start_serve.sh 的运行环境。"""
        env = dict(os.environ)
        cuda_home = self._venv_dir / "lib" / "python3.12" / "site-packages" / "nvidia" / "cu13"
        venv_bin = str(self._venv_dir / "bin")
        # torch 默认 FASTEST_FIRST 会把 Pro 6000 排到 cuda:0;PCI_BUS_ID + UUID 双保险钉 3090。
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["CUDA_VISIBLE_DEVICES"] = self._gpu_uuid
        env["CUDA_HOME"] = str(cuda_home)
        # venv/bin 找 ninja / sgl-omni;cu13/bin 找 nvcc(冷启现编 sgl-kernel)。
        env["PATH"] = f"{venv_bin}:{cuda_home / 'bin'}:{env.get('PATH', '')}"
        # 链接/运行期找 libcudart 等。
        ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{cuda_home / 'lib'}:{ld}" if ld else str(cuda_home / "lib")
        # 模型本地全量 + trust_remote_code,不联网;强制 HF 离线免经代理探 hub 拖慢冷启。
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        # 本机 mihomo 代理拦 127.0.0.1 回环(backend→微服务同机),排除。
        env["NO_PROXY"] = "127.0.0.1,localhost"
        return env

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def load(self, device: str | None = None) -> None:
        """起 sgl-omni serve 子进程并等健康。

        注意:**忽略 device index** —— GPU 钉卡由 self._gpu_uuid(env CUDA_VISIBLE_DEVICES)
        负责,不用 ModelManager 传进来的 cuda:N(见模块 docstring 差异点 2)。
        """
        # 已在配置端口跑着(如上次未清干净)→ 直接接管,不重复 spawn。
        if self._base_url and await self._health_check():
            self._model = True
            self._managed = False
            logger.info("Connected to existing MOSS sgl-omni at %s", self._base_url)
            return

        port = self._port or self._free_port()
        self._port = port
        self._base_url = f"http://127.0.0.1:{port}"
        self.base_url = self._base_url

        argv = self._build_argv(port)
        env = self._build_env()
        logger.info(
            "Starting MOSS sgl-omni on GPU %s port %d: %s",
            self._gpu_uuid, port, " ".join(argv),
        )

        self._process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(self._config_path.parent),  # config 里相对路径(如无)以此为基
            start_new_session=True,  # 独立进程组 → unload 时 killpg 收整组(含 worker)
        )
        self._managed = True
        self._stdout_tail.clear()
        self._drain_thread = threading.Thread(
            target=self._drain_stdout, name=f"moss-stdout-{port}", daemon=True
        )
        self._drain_thread.start()

        start = time.monotonic()
        last_log = 0
        try:
            while time.monotonic() - start < self._startup_timeout:
                if self._process.poll() is not None:
                    if self._drain_thread is not None:
                        self._drain_thread.join(timeout=1.0)
                    output = "".join(self._stdout_tail)
                    logger.error(
                        "MOSS sgl-omni exited with code %d", self._process.returncode)
                    logger.error("MOSS output (last 500 chars): %s", output[-500:])
                    self._kill_process()
                    raise RuntimeError(f"MOSS sgl-omni failed to start: {output[-200:]}")

                if await self._health_check():
                    elapsed = int(time.monotonic() - start)
                    self._model = True
                    logger.info("MOSS sgl-omni ready in %ds at %s", elapsed, self._base_url)
                    return

                elapsed_now = int(time.monotonic() - start)
                if elapsed_now - last_log >= 30:
                    last_log = elapsed_now
                    logger.info(
                        "MOSS sgl-omni still starting... (%ds elapsed, timeout %ds)",
                        elapsed_now, self._startup_timeout,
                    )
                await asyncio.sleep(3)

            logger.error(
                "MOSS sgl-omni did not become healthy within %ds", self._startup_timeout)
            self._kill_process()
            raise RuntimeError(
                f"MOSS sgl-omni did not become healthy within {self._startup_timeout}s")
        except Exception:
            self._kill_process()
            raise

    def unload(self) -> None:
        """杀整进程组释放显存;外部接管的实例只断连不杀。"""
        if self._managed and self._process is not None:
            logger.info("Unloading MOSS sgl-omni: killing process group (port %s)", self._port)
            self._kill_process()
            logger.info("MOSS sgl-omni process group killed, GPU memory released")
        else:
            logger.info("Disconnecting from external MOSS sgl-omni at %s", self._base_url)
        self._model = None
        self._close_client()

    def _kill_process(self) -> None:
        import signal

        from src.services.safe_signal import safe_killpg

        # 杀我们自己 spawn 的整组。safe_killpg 拒 pgid<=1(广播防护:即便自己的子进程,
        # 若已死 + PID 回收也可能解析出坏 pgid,killpg 会一锅端 mihomo/sshd)。
        if self._process is None:
            return
        try:
            if safe_killpg(self._process.pid, signal.SIGTERM):
                try:
                    # 等进程组退出 = 等显存释放;超时升级 SIGKILL。
                    self._process.wait(timeout=self._kill_timeout)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "MOSS sgl-omni 未在 %ds 内退出,升级 SIGKILL", self._kill_timeout)
                    safe_killpg(self._process.pid, signal.SIGKILL)
                    self._process.wait(timeout=5)
        except Exception as e:  # noqa: BLE001 — kill best-effort,不冒泡挡 unload
            logger.warning("Error killing MOSS sgl-omni subprocess: %s", e)
        finally:
            self._process = None

    def _close_client(self) -> None:
        """关 httpx client 连接池(同 vLLM/SGLang adapter):unload 丢弃 adapter 却不 aclose
        → 每轮 load/unload 泄漏一个 AsyncClient。unload 是 sync 且可能在 to_thread 线程跑。"""
        client = getattr(self, "_client", None)
        if client is None:
            return
        self._client = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is not None and loop.is_running():
                loop.create_task(client.aclose())
            else:
                asyncio.run(client.aclose())
        except Exception:  # noqa: BLE001 — 关闭 best-effort
            pass

    def _drain_stdout(self) -> None:
        """后台线程:持续读子进程 stdout 进有界 deque,防 PIPE 填满阻塞(同 vLLM adapter)。"""
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._stdout_tail.append(line)
        except Exception:  # noqa: BLE001
            pass

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def _health_check(self) -> bool:
        client = getattr(self, "_client", None)
        if client is None or not self._base_url:
            return False
        try:
            resp = await client.get(f"{self._base_url}{self._health_path}", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    async def infer(self, req: InferenceRequest) -> InferenceResult:
        """未实现:MOSS 转写经端点直连 base_url 的 /v1/audio/transcriptions HTTP,
        不走 adapter.infer(同 chat/embeddings 直连 vLLM base_url)。"""
        raise NotImplementedError(
            "SGLangOmniAdapter 不经 infer():ASR 端点直连 base_url 的 "
            "/v1/audio/transcriptions(见 openai_compat._asr_moss_transcribe)"
        )
