from ipaddress import ip_address

from aiohttp import web
from server import PromptServer


@PromptServer.instance.routes.post("/nous/clear-execution-cache")
async def clear_execution_cache(request):
    try:
        local = ip_address(request.remote or "").is_loopback
    except ValueError:
        local = False
    if not local or request.headers.get("X-Nous-Action") != "clear-execution-cache":
        raise web.HTTPForbidden()

    queue = PromptServer.instance.prompt_queue
    with queue.mutex:
        flags = queue.get_flags(reset=False)
        if queue.get_tasks_remaining() or flags.get("unload_models", flags.get("free_memory", False)):
            raise web.HTTPConflict()
        # The worker defaults unload_models to free_memory if the flag is absent.
        queue.set_flag("unload_models", False)
        queue.set_flag("free_memory", True)
    return web.Response(status=202)


NODE_CLASS_MAPPINGS = {}
