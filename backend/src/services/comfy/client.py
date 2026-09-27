"""ComfyUI sidecar HTTP 客户端(spec §5)。所有请求 trust_env=False。"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from collections.abc import Awaitable, Callable

import httpx


def _base_url() -> str:
    return os.getenv("NOUS_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")


# ComfyUI-Easy-Use 的风格缩略图路由。代理端点只打这一条(见 ComfyClient.style_image)。
STYLE_IMAGE_ROUTE = "/easyuse/prompt/styles/image"


class ComfyError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def translate_prompt_error(status: int, body: str) -> str:
    """ComfyUI /prompt 校验 payload → 可操作中文(仿 IC comfy_prompt_error_message)。"""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return f"ComfyUI 请求失败(HTTP {status}):{body[:300] or '未知错误'}"
    parts: list[str] = []
    top = (data.get("error") or {}).get("message") or ""
    for node_id, ne in (data.get("node_errors") or {}).items():
        ct = ne.get("class_type") or ""
        for err in ne.get("errors") or []:
            inp = (err.get("extra_info") or {}).get("input_name") or ""
            parts.append(f"节点 {node_id}({ct}) 输入 {inp}:{err.get('message', '')}")
    detail = ";".join(parts) or top
    return f"ComfyUI 拒绝了工作流(HTTP {status}):{detail[:600] or '校验失败'}。请检查模板字段映射。"


class ComfyClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or _base_url()).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url, trust_env=False,
            timeout=float(os.getenv("NOUS_COMFY_DOWNLOAD_TIMEOUT", "120")))

    async def health(self) -> dict:
        try:
            r = await self._client.get("/queue", timeout=3)
            q = r.json()
            depth = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
            ver = ""
            try:
                ver = (await self._client.get("/system_stats", timeout=3)).json() \
                    .get("system", {}).get("comfyui_version", "")
            except (httpx.HTTPError, ValueError):
                pass
            return {"online": True, "queue_depth": depth, "version": ver}
        except (httpx.HTTPError, ValueError):
            return {"online": False, "queue_depth": 0, "version": ""}

    async def object_info(self) -> dict:
        return (await self._client.get("/object_info", timeout=15)).json()

    async def node_info(self, class_type: str, *, timeout: float = 5.0) -> dict | None:
        """单个节点类型的 object_info(`/object_info/{class_type}`)。

        比整份 `/object_info`(几 MB)轻得多,桥剪枝只要几个下游节点的输入声明。
        sidecar 不认识这个类型时返回 `{}` → 这里返回 None;HTTP 非 200 抛 ComfyError。
        类名里有空格/括号(`Image Comparer (rgthree)`),按路径段整体转义。
        """
        r = await self._client.get(
            f"/object_info/{urllib.parse.quote(class_type, safe='')}", timeout=timeout)
        if r.status_code != 200:
            raise ComfyError(f"读取节点信息失败(HTTP {r.status_code}):{class_type}")
        data = r.json()
        info = data.get(class_type) if isinstance(data, dict) else None
        return info if isinstance(info, dict) else None

    async def styles(self, pack: str, *, timeout: float = 15.0) -> list[dict]:
        """ComfyUI-Easy-Use 的风格清单(`/easyuse/prompt/styles?name=<包>`)。

        返回项形如 `{name, name_cn, thumbnail, prompt, negative_prompt}`。thumbnail
        对 fooocus_styles 是 GitHub raw 外链(浏览器直接能加载),对 krea2 那批包是
        **sidecar 侧的相对路径**(`/easyuse/prompt/styles/image?path=…`)—— 后者浏览器
        会按 nous 的 origin 解析必 404,由路由层 `_style_to_option` 改写成走
        `/api/v1/comfy/style-image` 代理(见 comfy_templates.py)。
        """
        r = await self._client.get(
            "/easyuse/prompt/styles", params={"name": pack}, timeout=timeout)
        if r.status_code != 200:
            raise ComfyError(f"读取风格清单失败(HTTP {r.status_code})")
        data = r.json()
        return data if isinstance(data, list) else []

    async def style_image(self, path: str) -> tuple[bytes, str]:
        """取一张风格缩略图。`path` 是 sidecar 缩略图 URL 里 `path=` 那个值
        (如 `./samples/x.jpg`),**不是**整条 URL。

        krea2 那批风格包的 `thumbnail` 是**相对路径**(fooocus 包才是 GitHub 外链)——
        浏览器拿到会按 nous 自己的 origin 解析,必 404。所以要经后端代理一手。

        路由**写死**在这里、参数走 `params=`,绝不把调用方给的字符串当 URL 拼:
          · httpx 合并相对 URL 时会归一化点段 —— `/easyuse/../history` 直接变成
            `/history`(实测),任何"前缀白名单"都挡不住,代理会退化成"拿 admin
            身份任意打 sidecar GET"的跳板;
          · 当 URL 拼还会把文件名里的 `#` 当 fragment、`%`/`+` 当转义,
            `./samples/Neon #3.jpg` 这种名字的图直接取不回来。
        """
        r = await self._client.get(
            STYLE_IMAGE_ROUTE, params={"path": path}, timeout=30)
        if r.status_code != 200:
            raise ComfyError(f"读取风格缩略图失败(HTTP {r.status_code})")
        return r.content, r.headers.get("content-type", "image/jpeg")

    async def style_packs(self) -> list[str]:
        """可选的风格包清单 —— 从 object_info 的 `easy stylesSelector.styles` combo 取。

        没装 ComfyUI-Easy-Use(或节点改名)时返回空列表,由路由层决定怎么呈现。
        """
        oi = await self.object_info()
        try:
            return list(oi["easy stylesSelector"]["input"]["required"]["styles"][1]["options"])
        except (KeyError, IndexError, TypeError):
            return []

    async def system_stats(self) -> dict:
        """VRAM/设备快照,同 health() 降级——瞬断不应打断健康面板渲染。"""
        try:
            return (await self._client.get("/system_stats", timeout=3)).json()
        except (httpx.HTTPError, ValueError):
            return {}

    async def free(self, *, unload_models: bool = True, free_memory: bool = True) -> None:
        """用户触发的显存释放——失败必须冒泡(不像 health/system_stats 静默降级)。"""
        r = await self._client.post(
            "/free",
            json={"unload_models": unload_models, "free_memory": free_memory},
            timeout=30,
        )
        if r.status_code != 200:
            raise ComfyError(f"释放显存失败(HTTP {r.status_code})")

    async def upload_image(self, filename: str, content: bytes, mime: str = "image/png") -> str:
        r = await self._client.post(
            "/upload/image", files={"image": (filename, content, mime)}, timeout=60)
        if r.status_code != 200:
            raise ComfyError(f"上传参考素材失败(HTTP {r.status_code})")
        return r.json().get("name", filename)

    async def submit(self, graph: dict) -> str:
        r = await self._client.post("/prompt", json={"prompt": graph}, timeout=30)
        if r.status_code != 200:
            raise ComfyError(translate_prompt_error(r.status_code, r.text), status_code=422)
        return r.json()["prompt_id"]

    async def _history_entry(self, prompt_id: str) -> dict | None:
        """`/history/{id}` 里这条记录;不存在**或** sidecar 瞬断都返回 None
        (调用方一律当"还没出现",继续轮询)。"""
        try:
            res = (await self._client.get(f"/history/{prompt_id}", timeout=10)).json()
        except (httpx.HTTPError, ValueError):
            return None
        if isinstance(res, dict) and prompt_id in res:
            return res[prompt_id]
        return None

    async def _queue_has(self, prompt_id: str) -> bool | None:
        """prompt 还在 sidecar 队列里(running 或 pending)吗?

        返回 True/False,**未知返回 None** —— sidecar 不可达(重启窗口里正是如此)、
        返回体不是 dict、或两个队列键一个都没有(不认得的响应形状)都算未知,调用方
        不能据此判"任务没了"。

        `/queue` 的 `queue_running` / `queue_pending` 是列表,每项形如
        `[number, prompt_id, graph, extra_data, outputs]` —— prompt_id 在下标 1。
        """
        try:
            q = (await self._client.get("/queue", timeout=10)).json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(q, dict) or ("queue_running" not in q and "queue_pending" not in q):
            return None
        for bucket in ("queue_running", "queue_pending"):
            for item in q.get(bucket) or []:
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
                    return True
        return False

    async def wait(
        self,
        prompt_id: str,
        *,
        timeout_s: float,
        interval_s: float = 2.0,
        should_abort: Callable[[], Awaitable[bool]] | None = None,
        abort_check_every: int = 5,
        missing_grace_rounds: int = 3,
    ) -> dict:
        """轮询到 `/history/{prompt_id}` 出现为止,期间兼顾两件事(2026-09-03 事故)。

        1. **取消**:`should_abort()` 返回 True 就抛 `ComfyError("渲染已取消")`。
           调用方(桥节点)传的是"这个 ExecutionTask 在 DB 里是不是 cancelled"。
           **每 `abort_check_every` 轮才查一次**(默认 5 × 2s = 10s):渲染动辄几十
           分钟、上限 `NOUS_COMFY_TIMEOUT` 默认 14400s,每 2s 打一次 DB 就是 7200 次
           纯轮询查询;而"取消晚 10 秒被发现"对用户无差别 —— cancel 端点已经先落
           cancelled 再转发 `/interrupt`(routes/predictions.py),这条检查只是兜住
           "`/interrupt` 拉不动 ComfyUI"(它只在节点边界生效,卡在某节点内部的下载/
           网络等待救不回来)的情况。
        2. **sidecar 重启 / 队列被清**:prompt 既不在 `/queue` 也永远不会进
           `/history` —— 不检测的话这个 wait 会占着渲染信号量干等到 4 小时超时,
           后面所有 comfy 服务全堵死(正是 2026-09-03 线上事故的形态)。连续
           `missing_grace_rounds` 轮"队列里没有"才判丢失(刚 submit 完有个极短窗口
           两边都还没有),判之前再确认一次 history(任务可能正好在两次请求之间跑完)。
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        rounds = 0
        missing_rounds = 0
        while True:
            if (
                should_abort is not None
                and rounds % abort_check_every == 0
                and await should_abort()
            ):
                raise ComfyError("渲染已取消")

            history = await self._history_entry(prompt_id)
            if history is not None:
                return history

            if await self._queue_has(prompt_id) is False:
                missing_rounds += 1
                if missing_rounds >= missing_grace_rounds:
                    history = await self._history_entry(prompt_id)
                    if history is not None:
                        return history
                    raise ComfyError("ComfyUI 侧任务已丢失(sidecar 可能重启过)")
            else:
                missing_rounds = 0  # 回到队列里 / 状态未知 → 重新计数

            if loop.time() >= deadline:
                raise ComfyError("ComfyUI 渲染超时(NOUS_COMFY_TIMEOUT)。注意:ComfyUI 侧任务可能仍在运行。")
            await asyncio.sleep(interval_s)
            rounds += 1

    async def download(self, item: dict) -> bytes:
        qs = urllib.parse.urlencode({
            "filename": item["filename"], "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output")})
        r = await self._client.get(f"/view?{qs}")
        if r.status_code != 200:
            raise ComfyError(f"下载产物失败(HTTP {r.status_code}):{item['filename']}")
        return r.content

    async def interrupt(self) -> None:
        try:
            await self._client.post("/interrupt", timeout=5)
        except httpx.HTTPError:
            pass  # 尽力而为:sidecar 掉线时取消不应抛错


# I4 fix:进程级懒单例——`comfy_bridge.py` / `comfy_templates.py` 此前各自的
# `get_client()` 都是「每次调用现建一个 ComfyClient」,每建一个就新开一个
# httpx.AsyncClient(+ 连接池),旧的从不关闭,纯泄漏。改成单进程共享一个实例。
# 两个模块各自 `from ... import get_comfy_client as get_client` 保留各自的
# `get_client` 名字(测试按模块 monkeypatch.setattr(mod, "get_client", ...) 的
# 惯例不变——patch 的是各自模块命名空间里的这个名字,互不影响)。
_singleton: ComfyClient | None = None


def get_comfy_client() -> ComfyClient:
    global _singleton
    if _singleton is None:
        _singleton = ComfyClient()
    return _singleton


def reset_comfy_client() -> None:
    """测试/重配置用:丢弃缓存的单例,下次 get_comfy_client() 重新构造。"""
    global _singleton
    _singleton = None
