"""Private HTTP worker. No browser port, cookie or credential reaches the Canvas."""
import asyncio
import hmac
import socket
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, StreamingResponse
from playwright.async_api import Error as PlaywrightError
from .commands import execute
from .protocol import Command
from .session import BrowserSession

def create_app(config):
    session = BrowserSession(config)
    async def authorize(request: Request):
        token = request.headers.get("x-hugagent-resource-token", "")
        if not config.get("token") or not hmac.compare_digest(token, config["token"]):
            raise HTTPException(401, "unauthorized")

    @asynccontextmanager
    async def lifespan(app):
        await session.start()
        async def reap():
            while not session.closed:
                await asyncio.sleep(15)
                if not session.viewers and time.monotonic() - session.last_active > config.get("idle_seconds", 1800):
                    await session.close()
                    if config.get("_shutdown"):
                        config["_shutdown"]()
        task = asyncio.create_task(reap())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if not session.closed:
                await session.close()

    app = FastAPI(lifespan=lifespan, dependencies=[Depends(authorize)], docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/shutdown")
    async def shutdown():
        if not session.closed:
            await session.close()
        from fastapi.responses import JSONResponse
        return JSONResponse({"closed": True}, background=BackgroundTask(config.get("_shutdown", lambda: None)))

    @app.get("/state")
    async def state():
        return await session.state()

    @app.post("/command")
    async def command(body: Command):
        try:
            return await execute(session, body)
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise HTTPException(504, "operation_timeout_result_unknown") from exc
        except PlaywrightError as exc:
            # Driver messages can contain private URLs or page text.
            raise HTTPException(409, "browser_operation_failed_result_unknown") from exc

    @app.get("/events")
    async def events():
        if session.closed:
            raise HTTPException(410, "session_closed")
        return StreamingResponse(session.subscribe(), media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    @app.get("/download/{download_id}")
    async def download(download_id: str):
        item = session.downloads.get(download_id)
        if item is None:
            raise HTTPException(404, "unknown_download")
        path = await item.path()
        if path is None:
            raise HTTPException(409, "download_failed")
        return FileResponse(path, filename=item.suggested_filename)

    return app

def main(config):
    import uvicorn
    sock = socket.socket()
    sock.bind(("0.0.0.0", int(config.get("port", 0))))
    sock.listen(128)
    print('{"runtime_ready": true, "port": ' + str(sock.getsockname()[1]) + '}', flush=True)
    server = uvicorn.Server(uvicorn.Config(create_app(config), log_level="warning", access_log=False, lifespan="on", loop="asyncio"))
    config["_shutdown"] = lambda: setattr(server, "should_exit", True)
    server.run(sockets=[sock])
