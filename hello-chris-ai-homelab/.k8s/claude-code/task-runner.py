"""
Claude Code Task Runner – lightweight FastAPI wrapper around `claude -p`.

Endpoints
---------
POST /tasks          Submit a new task ({"prompt": "..."})
GET  /tasks          List all tasks
GET  /tasks/{id}     Get task detail
POST /tasks/{id}/cancel  Cancel a running task

The runner executes one task at a time.  Additional submissions are queued.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("task-runner")

WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
MAX_RESULT_CHARS = 50_000  # truncate huge outputs

app = FastAPI(title="Claude Code Task Runner")

# ---------------------------------------------------------------------------
# In-memory task store
# ---------------------------------------------------------------------------
tasks: dict[str, dict] = {}
task_queue: asyncio.Queue = asyncio.Queue()
_worker_started = False


class TaskRequest(BaseModel):
    prompt: str


# ---------------------------------------------------------------------------
# Worker – pulls from queue, runs one task at a time
# ---------------------------------------------------------------------------
async def _worker():
    while True:
        task_id = await task_queue.get()
        t = tasks.get(task_id)
        if not t or t["status"] == "cancelled":
            task_queue.task_done()
            continue
        t["status"] = "running"
        t["started_at"] = datetime.now(timezone.utc).isoformat()
        log.info("task %s running: %s", task_id, t["prompt"][:120])
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", t["prompt"],
                "--output-format", "json",
                "--max-turns", "25",
                "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=WORKSPACE,
            )
            t["_proc"] = proc
            stdout, stderr = await proc.communicate()
            del t["_proc"]
            stdout_str = stdout.decode(errors="replace")[:MAX_RESULT_CHARS]
            stderr_str = stderr.decode(errors="replace")[:MAX_RESULT_CHARS]
            if proc.returncode == 0:
                t["status"] = "completed"
                try:
                    t["result"] = json.loads(stdout_str)
                except Exception:
                    t["result"] = stdout_str
            else:
                t["status"] = "failed"
                t["error"] = stderr_str or f"exit code {proc.returncode}"
                t["result"] = stdout_str or None
        except Exception as exc:
            t["status"] = "failed"
            t["error"] = str(exc)
        t["finished_at"] = datetime.now(timezone.utc).isoformat()
        log.info("task %s %s", task_id, t["status"])
        task_queue.task_done()


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        asyncio.get_event_loop().create_task(_worker())
        _worker_started = True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/tasks")
async def submit_task(body: TaskRequest):
    _ensure_worker()
    task_id = uuid.uuid4().hex[:8]
    tasks[task_id] = {
        "id": task_id,
        "prompt": body.prompt,
        "status": "queued",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    await task_queue.put(task_id)
    log.info("task %s queued: %s", task_id, body.prompt[:120])
    return {"task_id": task_id, "status": "queued"}


@app.get("/tasks")
async def list_tasks():
    return [
        {
            "id": t["id"],
            "prompt": t["prompt"][:200],
            "status": t["status"],
            "submitted_at": t["submitted_at"],
            "finished_at": t.get("finished_at"),
        }
        for t in tasks.values()
    ]


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    safe = {k: v for k, v in t.items() if k != "_proc"}
    return safe


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if t["status"] in ("completed", "failed", "cancelled"):
        return {"id": task_id, "status": t["status"], "message": "already finished"}
    t["status"] = "cancelled"
    proc = t.get("_proc")
    if proc and proc.returncode is None:
        proc.terminate()
    return {"id": task_id, "status": "cancelled"}


DOWNLOADABLE_EXTS = {".pdf", ".md", ".txt", ".json", ".csv", ".html", ".yaml", ".yml", ".py", ".sh", ".png", ".jpg", ".svg"}

@app.get("/files")
async def list_files():
    """List files in the workspace that were created or modified, filtered to useful extensions."""
    ws = Path(WORKSPACE)
    files = []
    for p in sorted(ws.rglob("*")):
        if p.is_file() and p.suffix.lower() in DOWNLOADABLE_EXTS and ".git" not in p.parts:
            try:
                stat = p.stat()
                files.append({
                    "path": str(p.relative_to(ws)),
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                })
            except Exception:
                continue
    return {"workspace": WORKSPACE, "count": len(files), "files": files}


@app.get("/files/{file_path:path}")
async def download_file(file_path: str):
    """Download a file from the workspace."""
    ws = Path(WORKSPACE)
    full = (ws / file_path).resolve()
    # Security: ensure path is under workspace
    if not str(full).startswith(str(ws.resolve())):
        raise HTTPException(status_code=403, detail="path traversal blocked")
    if not full.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(full, filename=full.name)


@app.get("/health")
async def health():
    return {"status": "ok"}
