"""容器与 nb-cli 共用的启动入口。"""

import nonebot
import asyncio
from contextlib import suppress
from src.connection_health import qq_online, watch_connections
from nonebot.adapters.onebot.v11 import Adapter


nonebot.init()
nonebot.get_driver().register_adapter(Adapter)
nonebot.load_from_toml("pyproject.toml")
app = nonebot.get_app()
connection_watch = None


@nonebot.get_driver().on_startup
async def start_connection_watch():
    global connection_watch
    connection_watch = asyncio.create_task(watch_connections(nonebot.get_bots, nonebot.logger))


@nonebot.get_driver().on_shutdown
async def stop_connection_watch():
    if connection_watch is not None:
        connection_watch.cancel()
        with suppress(asyncio.CancelledError):
            await connection_watch


@app.get("/healthz", include_in_schema=False)
async def healthz():
    # 这里只检查 HTTP 服务就绪；QQ 连接由 NapCat 建立，不能作为启动依赖。
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    from fastapi.responses import JSONResponse
    online = await qq_online(nonebot.get_bots().values())
    return JSONResponse({"qq_connected": online}, status_code=200 if online else 503)


if __name__ == "__main__":
    nonebot.run()
