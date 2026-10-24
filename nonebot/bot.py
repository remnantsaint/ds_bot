"""容器与 nb-cli 共用的启动入口。"""

import nonebot
import asyncio
from contextlib import suppress
from nonebot.adapters.onebot.v11 import Adapter


nonebot.init()
nonebot.get_driver().register_adapter(Adapter)
nonebot.load_from_toml("pyproject.toml")
app = nonebot.get_app()
memory_watch = None
from src.message_recovery import connected
nonebot.get_driver().on_bot_connect(connected)


@nonebot.get_driver().on_startup
async def start_memory_worker():
    global memory_watch
    from src.memory import memory_worker
    memory_watch = asyncio.create_task(memory_worker())


@nonebot.get_driver().on_shutdown
async def stop_background_tasks():
    from src.message_recovery import stop
    await stop()
    if memory_watch is not None:
        memory_watch.cancel()
        with suppress(asyncio.CancelledError):
            await memory_watch


@app.get("/healthz", include_in_schema=False)
async def healthz():
    # 这里只检查 HTTP 服务就绪；QQ 连接由 NapCat 建立，不能作为启动依赖。
    return {"status": "ok"}


if __name__ == "__main__":
    nonebot.run()
