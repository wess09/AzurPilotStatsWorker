import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

from .config import Config
from .db import Database
from .engine import Engine
from .db import validate_source
from .uploader import UploaderConnection


def create_app(config=None, start_engine=True):
    config = config or Config.load()
    config.data.mkdir(parents=True, exist_ok=True)
    db = Database(config.data / "state.sqlite", config.file_bytes, config.group_bytes)
    uploader = UploaderConnection(config, db)

    @asynccontextmanager
    async def lifespan(app):
        engine = Engine(config, db) if start_engine else None
        app.state.engine = engine
        task = asyncio.create_task(engine.run()) if engine else None
        connection = asyncio.create_task(uploader.run()) if config.uploader_url else None
        try:
            yield
        finally:
            for running in (connection, task):
                if not running:
                    continue
                running.cancel()
                try:
                    await running
                except asyncio.CancelledError:
                    pass
            db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    icon_slots = asyncio.Semaphore(2)

    @app.post("/internal/v1/icon-matches")
    async def icon_matches(request: Request):
        import json
        from fastapi import HTTPException
        if not config.worker_token or not hmac.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + config.worker_token):
            raise HTTPException(401)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 32 * 1024 * 1024:
                raise HTTPException(413, "Icon request exceeds 32 MiB")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get("icons"), list) or not 1 <= len(body["icons"]) <= 50:
                raise ValueError("Expected 1..50 icons")
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "Invalid icon matching request") from exc
        engine = request.app.state.engine
        if engine is None:
            raise HTTPException(503)
        async with icon_slots:
            try:
                return await asyncio.to_thread(engine.match_icons, body)
            except ValueError as exc:
                raise HTTPException(400, "Invalid icon matching data") from exc

    @app.post("/internal/v1/preview")
    def preview(body: dict, request: Request):
        from fastapi import HTTPException
        from fastapi.responses import FileResponse
        from starlette.background import BackgroundTask
        from pathlib import Path
        from PIL import Image
        import tempfile
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + config.worker_token):
            raise HTTPException(401)
        try:
            source = body["source"]
            validate_source(source, config.file_bytes, config.group_bytes)
            index, frame = body["file_index"], body["frame_index"]
            if type(index) is not int or type(frame) is not int or not 0 <= index < len(source["files"]) or not 0 <= frame < 10:
                raise ValueError("Invalid frame")
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        engine = request.app.state.engine
        if engine is None:
            raise HTTPException(503)
        temporary = tempfile.TemporaryDirectory(dir=engine.work)
        try:
            path = Path(temporary.name) / "source.png"
            engine.pan.download(source["files"][index], path)
            target = Path(temporary.name) / "frame.png"
            with Image.open(path) as image:
                if image.width != 1280 or image.height % 720 or image.height > 7200 or (frame + 1) * 720 > image.height:
                    raise ValueError("Unsupported image size")
                image.crop((0, frame * 720, 1280, (frame + 1) * 720)).save(target)
            path.unlink()
            return FileResponse(target, media_type="image/png", headers={"Cache-Control": "no-store"}, background=BackgroundTask(temporary.cleanup))
        except Exception as exc:
            temporary.cleanup()
            raise HTTPException(502, "Original preview unavailable") from exc

    @app.get("/healthz")
    def health():
        engine = getattr(app.state, 'engine', None)
        return {"status": "ok", "pending": db.pending(), "uploader_connected": uploader.connected,
                "stages": db.stage_counts(),
                "recognition_workers": engine.workers if engine else 0,
                "job_slots": engine.recognize_slots if engine else 0,
                "pipeline": config.pipeline,
                "performance": engine.performance() if engine else None}

    @app.websocket("/internal/v1/batches")
    async def receiver(ws: WebSocket):
        if not config.worker_token or not hmac.compare_digest(ws.headers.get("authorization", ""), "Bearer " + config.worker_token):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                batch = await ws.receive_json()
                try:
                    db.accept(batch)
                except (ValueError, KeyError, TypeError) as exc:
                    logging.warning("Rejected batch: %s", exc)
                    await ws.close(code=1008)
                    return
                await ws.send_json({"type": "batch_accepted", "batch_id": batch["batch_id"]})
        except WebSocketDisconnect:
            return

    app.state.db = db
    return app


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    # Download redirects contain temporary cloud credentials in their URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(), host=os.getenv("LISTEN_HOST", "127.0.0.1"), port=int(os.getenv("LISTEN_PORT", "8091")), ws_max_size=16 * 1024**2, ws_ping_interval=25, ws_ping_timeout=10)


if __name__ == "__main__":
    main()
