"""Runner HTTP endpoints for resumable, non-PTY commands."""

import codecs
import os
import json
import signal
import tempfile
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

if __package__:
    from .process_sessions import ProcessSessions, ProcessSessionError
else:
    from process_sessions import ProcessSessions, ProcessSessionError


def install(server):
    sessions = ProcessSessions()

    class StartRequest(server.ProcessRequest):
        timeout: int | None = None
        yield_time_ms: int = 60000
        capability_run_id: str | None = None
        capability_scope: str = ""

    class WriteRequest(BaseModel):
        sandbox_session_id: str
        user_id: str | None = None
        session_id: str
        chars: str = ""
        yield_time_ms: int = 60000

    @server.app.post("/processes/start")
    async def start(req: StartRequest):
        if req.language not in server.INTERPRETERS:
            raise HTTPException(400, f"Unsupported language: {req.language}")
        if len(req.script_content) > server.MAX_SCRIPT_SIZE:
            raise HTTPException(400, "Command is too large")
        root = server._session_workspace(
            req.session_id,
            create=True,
            user_id=req.user_id,
            capability_view_key=req.capability_view_key,
        )
        command = server._workspace_rules().execution_text(
            req.script_content,
            req.language,
            root,
            req.user_id,
        )

        server._validate_filename(req.script_name)
        for files in (req.resource_files, req.input_files, req.input_files_b64):
            for name in files or {}:
                server._validate_filename(name)
        seeded = set()
        server._seed_text_files(root, req.resource_files, seeded)
        server._seed_text_files(root, req.input_files, seeded)
        server._seed_b64_files(root, req.input_files_b64, seeded)
        params = dict(req.params)
        raw_args = params.pop("_args", [])
        args = (
            [
                server._workspace_rules().execution_text(str(arg), req.language, root, req.user_id)
                for arg in raw_args
            ]
            if isinstance(raw_args, list)
            else []
        )

        async def spawn():
            directory = Path(tempfile.mkdtemp(prefix=".__process_", dir=root))
            suffix = {"bash": ".sh", "python": ".py", "javascript": ".js"}[req.language]
            fd, name = tempfile.mkstemp(prefix=".__process_script_", suffix=suffix, dir=root)
            script = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(command)
            handle = LocalHandle(server, directory)
            handle.script = script
            if req.params or req.language != "bash":
                handle.stdin.close()
                stdin_path = directory / "stdin.json"
                stdin_path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
                handle.stdin = stdin_path.open("rb")
            handle.metadata = (
                {
                    "_capability": {
                        "run_id": req.capability_run_id,
                        "scope": req.capability_scope,
                        "user_id": req.user_id,
                    }
                }
                if req.capability_run_id
                else {}
            )
            try:
                handle.proc = await server._spawn_subprocess(
                    [*server.INTERPRETERS[req.language], str(script), *args],
                    str(root),
                    req.sandbox_launch,
                    (handle.stdin, handle.stdout, handle.stderr),
                )
                return handle
            except BaseException:
                await handle.close()
                raise

        try:
            return await sessions.start(
                spawn,
                (req.session_id, req.user_id or ""),
                max(250, min(req.yield_time_ms, 60000)),
                req.timeout,
            )
        except ProcessSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @server.app.post("/processes/write")
    async def write(req: WriteRequest):
        try:
            return await sessions.write(
                req.session_id,
                (req.sandbox_session_id, req.user_id or ""),
                req.chars,
                req.yield_time_ms,
            )
        except ProcessSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @server.app.on_event("shutdown")
    async def shutdown():
        await sessions.close_all()

    return sessions


class LocalHandle:
    def __init__(self, server, directory):
        self.server = server
        self.script = None
        self.proc = None
        self.stdin = open(os.devnull, "rb")
        self.stdout = (directory / "stdout.log").open("wb")
        self.stderr = (directory / "stderr.log").open("wb")
        self.readers = [(directory / name).open("rb") for name in ("stdout.log", "stderr.log")]
        self.decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in range(2)]
        self.log_paths = {name: str(directory / (name + ".log")) for name in ("stdout", "stderr")}

    async def poll(self):
        chunks = []
        for reader, decoder in zip(self.readers, self.decoders):
            if os.fstat(reader.fileno()).st_size > 64 * 1024 * 1024:
                raise ValueError("Command log exceeded 64 MiB; command stopped")
            chunks.append(decoder.decode(reader.read(262144)))
        code = self.proc.returncode
        # Drain remaining output before declaring completion.
        if code is not None and any(r.tell() < os.fstat(r.fileno()).st_size for r in self.readers):
            code = None
        if code is not None:
            chunks = [
                text + decoder.decode(b"", final=True)
                for text, decoder in zip(chunks, self.decoders)
            ]
        return chunks[0], chunks[1], code

    async def interrupt(self):
        if self.proc is None or self.proc.returncode is not None:
            return
        if os.name == "nt":
            # Headless Windows children have no console for CTRL_C_EVENT.
            await self.server._terminate_process_group(self.proc)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    async def close(self):
        await self.server._terminate_process_group(self.proc)
        close = getattr(self.proc, "close", None)
        if close:
            close()
        for stream in (self.stdin, self.stdout, self.stderr, *self.readers):
            stream.close()
        if self.script:
            self.script.unlink(missing_ok=True)
