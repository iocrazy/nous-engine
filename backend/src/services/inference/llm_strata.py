"""External Strata server adapter."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import subprocess
import time
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import httpx

from src.services.inference.base import (
    InferenceAdapter,
    InferenceRequest,
    InferenceResult,
    MediaModality,
    StreamEvent,
    TextRequest,
    UsageMeter,
)


class StrataAdapter(InferenceAdapter):
    """Connect ModelManager to an already-running local Strata server."""

    modality = MediaModality.TEXT
    estimated_vram_mb = 0

    def __init__(
        self,
        paths: dict[str, str],
        device: str = "cuda",
        *,
        strata_base_url: str,
        request_timeout: float = 300,
        _transport: httpx.AsyncBaseTransport | None = None,
        **kwargs: Any,
    ):
        super().__init__(paths=paths, device=device)
        self._base_url = strata_base_url.rstrip("/")
        self.base_url = self._base_url
        self.max_model_len = 4096
        self._validate_base_url()
        self._client: httpx.AsyncClient | None = httpx.AsyncClient(
            timeout=request_timeout,
            limits=httpx.Limits(max_connections=10),
            transport=_transport,
            trust_env=False,
        )

    def _validate_base_url(self) -> None:
        parsed = urllib.parse.urlparse(self._base_url)
        host = parsed.hostname
        try:
            is_loopback = host == "localhost" or ipaddress.ip_address(host or "").is_loopback
        except ValueError:
            is_loopback = False
        if (
            parsed.scheme != "http"
            or not is_loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "strata_base_url 必须是无认证、无路径的本机 HTTP 地址，"
                f"收到: {self._base_url}"
            )

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Strata adapter 已卸载")
        return self._client

    async def load(self, device: str | None = None) -> None:
        """Connect only when Strata is reachable and its model is already loaded."""
        self._validate_base_url()
        client = self._require_client()
        try:
            resp = await client.get(f"{self._base_url}/health", timeout=3)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"无法连接 Strata: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"Strata health check failed ({resp.status_code})")
        try:
            status = resp.json()
        except ValueError as exc:
            raise RuntimeError("Strata /health 返回了无效 JSON") from exc
        if status.get("service") != "strata":
            raise RuntimeError("目标端点不是 Strata 服务")
        if status.get("loaded") is not True:
            raise RuntimeError("Strata 模型尚未加载；请先在 Strata 侧加载")
        max_context = status.get("max_context")
        if isinstance(max_context, int) and not isinstance(max_context, bool) and max_context > 0:
            self.max_model_len = max_context
        self._model = True

    def unload(self) -> None:
        """Disconnect without unloading or stopping the external Strata server."""
        self._model = None
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is not None and loop.is_running():
                loop.create_task(client.aclose())
            else:
                asyncio.run(client.aclose())
        except Exception:  # noqa: BLE001
            pass

    def _build_payload(self, req: TextRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": [message.model_dump(mode="json") for message in req.messages],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            **req.extra,
        }
        payload.setdefault("reasoning_effort", "high" if req.enable_thinking else "none")
        return payload

    async def infer(self, req: InferenceRequest) -> InferenceResult:
        if not isinstance(req, TextRequest):
            raise TypeError(f"StrataAdapter expects TextRequest, got {type(req).__name__}")
        self._validate_base_url()
        client = self._require_client()
        started = time.monotonic()
        resp = await client.post(
            f"{self._base_url}/v1/chat/completions",
            json=self._build_payload(req),
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text[:300])
            except ValueError:
                detail = resp.text[:300]
            raise RuntimeError(f"Strata API error ({resp.status_code}): {detail}")

        body = resp.json()
        if isinstance(body, dict) and (body.get("object") == "error" or body.get("error")):
            error = body.get("message") or body.get("error") or "unknown error"
            if isinstance(error, dict):
                error = error.get("message") or str(error)
            raise RuntimeError(f"Strata API error (200 body): {error}")
        raw_usage = body.get("usage") or {}
        return InferenceResult(
            media_type="application/json",
            data=resp.content,
            metadata={"raw": body},
            usage=UsageMeter(
                input_tokens=raw_usage.get("prompt_tokens"),
                output_tokens=raw_usage.get("completion_tokens"),
                latency_ms=latency_ms,
            ),
        )

    async def infer_stream(self, req: InferenceRequest) -> AsyncIterator[StreamEvent]:
        if not isinstance(req, TextRequest):
            raise TypeError(f"StrataAdapter expects TextRequest, got {type(req).__name__}")
        self._validate_base_url()
        client = self._require_client()
        payload = self._build_payload(req)
        payload["stream"] = True
        stream_options = dict(payload.get("stream_options") or {})
        stream_options["include_usage"] = True
        payload["stream_options"] = stream_options

        last_usage: dict[str, Any] | None = None
        async with client.stream(
            "POST",
            f"{self._base_url}/v1/chat/completions",
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield StreamEvent(
                    type="error",
                    payload={
                        "status_code": resp.status_code,
                        "body": body[:300].decode("utf-8", errors="replace"),
                    },
                )
                return
            try:
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    text = line[5:].strip()
                    if text == "[DONE]":
                        break
                    try:
                        chunk = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        last_usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    delta = choices[0].get("delta") if choices else None
                    if delta:
                        yield StreamEvent(type="delta", payload=delta)
            except httpx.HTTPError as exc:
                yield StreamEvent(
                    type="error",
                    payload={
                        "error": f"stream interrupted: {type(exc).__name__}: {exc}",
                        "usage": last_usage or {},
                    },
                )
                return
        yield StreamEvent(type="done", payload={"usage": last_usage or {}})


_STRATA_INSTANCE_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_STRATA_CONTROL_HELPER = "/usr/local/lib/nous-engine/strata-control"


class StrataSystemdController:
    """Call the root-owned, argument-validating Strata service helper."""

    def __init__(
        self,
        instance: str,
        *,
        command_timeout: float = 120,
        _run: Any = subprocess.run,
    ) -> None:
        if _STRATA_INSTANCE_RE.fullmatch(instance) is None:
            raise ValueError(
                "strata_instance 只允许小写字母、数字、下划线和连字符，长度 1-64"
            )
        self.instance = instance
        self.command_timeout = command_timeout
        self._run = _run

    def _invoke(self, action: str) -> None:
        try:
            result = self._run(
                ["sudo", "-n", _STRATA_CONTROL_HELPER, action, self.instance],
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Strata systemd {action} 调用失败: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "无输出").strip()[-500:]
            raise RuntimeError(
                f"Strata systemd {action} 失败(退出码 {result.returncode}): {detail}"
            )

    def start(self) -> None:
        self._invoke("start")

    def stop(self) -> None:
        self._invoke("stop")


class ManagedStrataAdapter(StrataAdapter):
    """Manage a dedicated Strata systemd instance and proxy its local API."""

    def __init__(
        self,
        paths: dict[str, str],
        device: str = "cuda",
        *,
        strata_base_url: str,
        strata_instance: str,
        startup_timeout: float = 900,
        startup_poll_interval: float = 2,
        shutdown_timeout: float = 120,
        _controller: StrataSystemdController | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            paths=paths,
            device=device,
            strata_base_url=strata_base_url,
            **kwargs,
        )
        if startup_timeout <= 0 or startup_poll_interval <= 0 or shutdown_timeout <= 0:
            raise ValueError("Strata 启停超时和轮询间隔必须大于 0")
        self._startup_timeout = startup_timeout
        self._startup_poll_interval = startup_poll_interval
        self._controller = _controller or StrataSystemdController(
            strata_instance,
            command_timeout=shutdown_timeout,
        )

    async def load(self, device: str | None = None) -> None:
        try:
            await asyncio.to_thread(self._controller.start)
        except Exception:
            await self._require_client().aclose()
            self._client = None
            raise
        deadline = time.monotonic() + self._startup_timeout
        last_error = "服务尚未就绪"
        try:
            while time.monotonic() < deadline:
                try:
                    await super().load(device)
                    return
                except RuntimeError as exc:
                    last_error = str(exc)
                await asyncio.sleep(self._startup_poll_interval)
            raise RuntimeError(
                f"Strata 在 {self._startup_timeout:g}s 内未就绪: {last_error}"
            )
        except BaseException:
            try:
                await asyncio.to_thread(self._controller.stop)
            except Exception:
                pass
            await self._require_client().aclose()
            self._client = None
            raise

    def unload(self) -> None:
        self._controller.stop()
        super().unload()
