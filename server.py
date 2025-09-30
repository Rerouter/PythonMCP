# server.py — Windows Tool Call + MCP (HTTP & HTTP+SSE) + OpenAI-compatible
from __future__ import annotations

import sys, os, json, time, asyncio, subprocess, threading
from pathlib import Path
from typing import Optional, Iterable, Dict, Any
import uuid

from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import hashlib, difflib, codecs

# -------------------- Config --------------------
SERVICE_NAME = "python"
SERVICE_VERSION = "1.3.3"
MCP_PROTOCOL = "2024-11-05"

PROJECT_ROOT = Path(__file__).resolve().parent  # or .parent.parent if needed
WORK_DIR = PROJECT_ROOT / "Work"
WORK_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TIMEOUT_S = 1200
DEFAULT_MEMORY_MB = 32000

N_SAMPLE_ROWS      = 1      # rows shown per table
MAX_CELL_LEN       = 120    # truncate long text cells
SQLITE_MAGIC       = b"SQLite format 3"

# For SSE sessions
SESSIONS: set[str] = set()
SESSION_QUEUES: Dict[str, "asyncio.Queue[dict[str, Any]]"] = {}

# --- ADD: simple artifact registry (top-level globals) ---
ARTIFACTS: dict[str, dict] = {}  # id -> {path, sha256, size}

# ---- fs.read policy knobs ----
FS_READ_CAP_BYTES = int(os.environ.get("FS_READ_CAP_BYTES", "20000"))
FS_READ_HARD_CAP_FULL = os.environ.get("FS_READ_HARD_CAP_FULL", "1") == "0"

# -------------------- Windows memory limiters --------------------
_has_win32 = False
try:
    import win32api, win32con, win32job  # type: ignore
    _has_win32 = True
except Exception:
    _has_win32 = False

_has_psutil = False
try:
    import psutil  # type: ignore
    _has_psutil = True
except Exception:
    _has_psutil = False

def _assign_job_memory_limit(pid: int, memory_mb: int):
    if not _has_win32:
        return None
    h_process = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
    h_job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(h_job, win32job.JobObjectExtendedLimitInformation)
    flags = info["BasicLimitInformation"]["LimitFlags"]
    flags |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info["BasicLimitInformation"]["LimitFlags"] = flags
    info["ProcessMemoryLimit"] = int(memory_mb) * 1024 * 1024
    win32job.SetInformationJobObject(h_job, win32job.JobObjectExtendedLimitInformation, info)
    win32job.AssignProcessToJobObject(h_job, h_process)
    return h_job

def _start_psutil_memory_watcher(proc: subprocess.Popen, memory_mb: int):
    if not _has_psutil:
        return None
    import time as _time
    limit = int(memory_mb) * 1024 * 1024
    def _watch():
        try:
            p = psutil.Process(proc.pid)
            while proc.poll() is None:
                if p.memory_info().rss > limit:
                    proc.kill()
                    break
                _time.sleep(0.1)
        except Exception:
            pass
    threading.Thread(target=_watch, daemon=True).start()
    return lambda: None

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _safe_rel_path(name: str | None) -> Path:
    """Map a user-provided relative path into WORK_DIR, preventing path escape."""
    if not name:
        return WORK_DIR / "user_code.py"
    p = (WORK_DIR / name).resolve()
    if not str(p).startswith(str(WORK_DIR.resolve()) + os.sep) and p != WORK_DIR.resolve():
        raise ValueError("Path escapes WORK_DIR")
    return p

def _mk_artifact(path: Path) -> dict:
    info = {
        "id": uuid.uuid4().hex[:12],
        "path": str(path),
        "sha256": _sha256_file(path),
        "size": path.stat().st_size if path.exists() else 0,
    }
    ARTIFACTS[info["id"]] = info
    return info

def _dir_listing() -> list[dict]:
    out = []
    for e in WORK_DIR.iterdir():
        if e.name.startswith("."):
            continue
        try:
            size = e.stat().st_size
        except Exception:
            size = None
        out.append({"name": e.name, "is_dir": e.is_dir(), "size": size})
    return sorted(out, key=lambda x: (not x["is_dir"], x["name"].lower()))

def _near_misses(name: str) -> list[str]:
    names = [x["name"] for x in _dir_listing()]
    return difflib.get_close_matches(name, names, n=5, cutoff=0.6)

def _validate_requires(reqs: list[str] | None) -> tuple[list[str], dict[str, list[str]]]:
    missing, suggestions = [], {}
    for r in (reqs or []):
        p = _safe_rel_path(r)
        if not p.exists():
            missing.append(r)
            suggestions[r] = _near_misses(r)
    return missing, suggestions

def _is_sqlite(path: Path) -> bool:
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return True
    try:
        with open(path, "rb") as f:
            return f.read(16).startswith(SQLITE_MAGIC)
    except OSError:
        return False

def _sample_sqlite(path: Path) -> dict:
    import sqlite3, itertools, math

    def _short(v):
        if isinstance(v, (bytes, memoryview)):
            return f"<BLOB {len(v)} bytes>"
        s = str(v)
        return (s[:MAX_CELL_LEN] + "…") if len(s) > MAX_CELL_LEN else s

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in cur.fetchall()]
    info = {"tables": []}

    for t in tables:
        cur.execute(f"PRAGMA table_info({t!r})")
        cols = [{"cid": r["cid"], "name": r["name"], "type": r["type"]} for r in cur.fetchall()]

        cur.execute(f"SELECT COUNT(*) FROM {t}")
        rowcount = cur.fetchone()[0]

        cur.execute(f"SELECT * FROM {t} LIMIT {N_SAMPLE_ROWS}")
        rows = [ {k: _short(v) for k,v in dict(r).items()} for r in cur.fetchall() ]

        info["tables"].append({
            "name": t,
            "columns": cols,
            "row_count": rowcount,
            "sample_rows": rows,
        })
    db.close()
    info["table_count"] = len(info["tables"])
    return info

# -------------------- Runner --------------------
def _run_python(code: str, timeout_s: int, memory_mb: int, stream: bool = False, *, filename: str | None = None) -> tuple[int, str, str] | Iterable[str]:
    """Runs code with -I in WORK_DIR; returns (rc, stdout, stderr) or an iterator for streaming."""
    code_file = _safe_rel_path(filename)  # <— NEW
    code_file.parent.mkdir(parents=True, exist_ok=True)
    code_file.write_text(code, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-s", "-E", str(code_file)],
        cwd=str(WORK_DIR),  # keep cwd stable
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
        creationflags=0,
    )
    h_job = None
    stop_watch = None
    try:
        if _has_win32:
            h_job = _assign_job_memory_limit(proc.pid, memory_mb)
        elif _has_psutil:
            stop_watch = _start_psutil_memory_watcher(proc, memory_mb)

        if not stream:
            try:
                out, err = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                return (-1, out or "", (err or "") + "\n[timeout]")
            return (proc.returncode or 0, out or "", err or "")

        start = time.time()
        stderr_acc: list[str] = []
        def _stderr_reader():
            try:
                if proc.stderr:
                    for line in proc.stderr:
                        stderr_acc.append(line)
            except Exception:
                pass
        threading.Thread(target=_stderr_reader, daemon=True).start()

        def _iter():
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        yield line
                        if time.time() - start > timeout_s:
                            proc.kill()
                            break
            finally:
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
                if stderr_acc:
                    yield "\n[STDERR]\n" + "".join(stderr_acc)
        return _iter()
    finally:
        try:
            if stop_watch: stop_watch()
        except Exception:
            pass
        try:
            if h_job: win32api.CloseHandle(h_job)
        except Exception:
            pass

# -------------------- Schemas --------------------
class CodeRequest(BaseModel):
    code: str
    timeout_s: Optional[int] = DEFAULT_TIMEOUT_S
    memory_mb: Optional[int] = DEFAULT_MEMORY_MB

class EvalResponse(BaseModel):
    stdout: str
    stderr: str
    returncode: int

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionsRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: Optional[bool] = False
    timeout_s: Optional[int] = DEFAULT_TIMEOUT_S
    memory_mb: Optional[int] = DEFAULT_MEMORY_MB

# -------------------- App & diagnostics --------------------
app = FastAPI(
    title="Code Execution API",
    description="Windows-only executor supporting MCP (HTTP & HTTP+SSE) and OpenAI-compatible endpoints.",
    version=SERVICE_VERSION,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def _diag(request: Request, call_next):
    body = await request.body()
    print(f"[REQ] {request.method} {request.url.path} CT={request.headers.get('content-type')} "
          f"Accept={request.headers.get('accept')} Len={len(body)}")
    if body:
        print("[REQ BODY]", body[:512].decode("utf-8", "ignore"))
    resp = await call_next(request)
    print(f"[RES] {resp.status_code} {request.url.path}")
    return resp

# -------------------- Helpers --------------------
def _service_info():
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "mcp": {"transport": "http+sse", "root": "/", "protocolVersion": MCP_PROTOCOL},
        "endpoints": ["/", "/healthz", "/eval", "/v1/models", "/v1/chat/completions", "/sse", "/message"],
    }

def _sse_event(data: dict | str, *, event: str | None = None, id_: str | None = None) -> bytes:
    # Build a standards-compliant SSE frame
    parts = []
    if event:
        parts.append(f"event: {event}\n")
    if id_:
        parts.append(f"id: {id_}\n")
    if isinstance(data, dict):
        payload = json.dumps(data, ensure_ascii=False)
    else:
        payload = data
    for line in str(payload).splitlines() or [""]:
        parts.append(f"data: {line}\n")
    parts.append("\n")
    return "".join(parts).encode("utf-8")

def _json_response(obj: dict, status_code: int = 200) -> Response:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        print("[RES BODY]", payload[:512].decode("utf-8", "ignore"))
    except Exception:
        pass
    return Response(content=payload, status_code=status_code, media_type="application/json")

# -------------------- Health & root --------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/")
async def root_get():
    return _service_info()

# -------------------- MCP core (build JSON-RPC envelopes as dicts) --------------------
def _jr_ok(id_, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def _jr_err(id_, code=-32601, message="Method not found", data=None) -> dict:
    e = {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}
    if data is not None:
        e["error"]["data"] = data
    return e

async def mcp_root_jsonrpc_dict(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
        return _jr_err(data.get("id") if isinstance(data, dict) else None, code=-32600, message="Invalid Request")

    method = data.get("method")
    rpc_id = data.get("id")
    params = data.get("params") or {}

    if method == "initialize":
        client_proto = (params or {}).get("protocolVersion") or MCP_PROTOCOL
        return _jr_ok(rpc_id, {
            "protocolVersion": client_proto,  # echo client’s proposed version
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "Py", "version": SERVICE_VERSION},
        })

    if method in ("notifications/initialized", "initialized"):
        # Notification → no response
        return {"__notification__": True}

    if method == "tools/list":
        tools = [
            {
                "name": "python_eval",
                "description": "Run Python in the workspace. You may specify a filename to save the file as, this will overwrite the file, and prerequisites.",
                "inputSchema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python source to execute"},
                        "filename": {"type": "string",
                                     "description": "Relative path to save/execute (default: user_code.py)"},
                        "requires": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files that must exist before running"
                        },
                        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 3600, "default": DEFAULT_TIMEOUT_S},
                        "memory_mb": {"type": "integer", "minimum": 256, "maximum": 50000, "default": DEFAULT_MEMORY_MB}
                    },
                    "required": ["code"],
                    "additionalProperties": False
                }
            },
            {
                "name": "fs.write_file",
                "description": "Write a file into the workspace and return an artifact.",
                "inputSchema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"}
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False
                }
            },
            {
                "name": "fs.read_file",
                "description": "Read a text file from the workspace (relative to WORK_DIR).",
                "inputSchema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to read"},
                        "encoding": {"type": "string", "description": "Text encoding (default: utf-8)"},
                        "max_bytes": {"type": "integer",
                                      "description": "0 to read the entire file, otherwise enter how many to sample"}
                    },
                    "required": ["path"],
                    "additionalProperties": False
                }
            },
            {
                "name": "fs.listdir",
                "description": "List files in the workspace directory.",
                "inputSchema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False
                }
            },
            {
                "name": "python.exec_bundle",
                "description": "Write multiple files atomically, then run an entrypoint.",
                "inputSchema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Map of {relative_path: content}"
                        },
                        "entrypoint": {"type": "string", "description": "File to execute (default: main.py)"},
                        "requires": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["files"],
                    "additionalProperties": False
                }
            }
        ]
        return _jr_ok(rpc_id, {"tools": tools})  # omit nextCursor when none

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "fs.write" or name == "fs.write_file":
            try:
                dest = _safe_rel_path(args.get("path"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(args.get("content", ""), encoding="utf-8")
                art = _mk_artifact(dest)
                resp = {
                    "ok": True,
                    "artifact": art,
                    "cwd": str(WORK_DIR),
                }
                return _jr_ok(rpc_id, {"content": [{"type": "text", "text": json.dumps(resp, indent=2)}], "isError": False})
            except Exception as e:
                err = {"ok": False, "error": str(e), "cwd": str(WORK_DIR)}
                return _jr_ok(rpc_id, {"content": [{"type": "text", "text": json.dumps(err, indent=2)}], "isError": True})

        if name == "fs.read" or name == "fs.read_file":
            args = params.get("arguments") or {}
            try:
                # resolve & validate path inside WORK_DIR
                p = _safe_rel_path(args.get("path"))
                if not p.exists() or not p.is_file():
                    raise FileNotFoundError(f"No such file: {p.relative_to(WORK_DIR)}")

                enc = args.get("encoding") or "utf-8"
                total = p.stat().st_size

                # Schema semantics: max_bytes==0 → full file; >0 → sample first N bytes
                max_bytes_arg = int(args.get("max_bytes", 0))
                if max_bytes_arg < 0:
                    raise ValueError("max_bytes must be >= 0")

                requested_mode = "full" if max_bytes_arg == 0 else "sample"
                cap = FS_READ_CAP_BYTES

                if requested_mode == "full":
                    if FS_READ_HARD_CAP_FULL and total > cap:
                        read_n = cap
                        cap_enforced = True
                    else:
                        read_n = total
                        cap_enforced = False
                else:
                    read_n = min(max_bytes_arg, cap)
                    cap_enforced = max_bytes_arg > cap

                if _is_sqlite(p):
                    payload = {
                        "ok": True,
                        "path": str(p.relative_to(WORK_DIR)),
                        "kind": "sqlite",
                        "summary": _sample_sqlite(p),
                    }
                    return _jr_ok(rpc_id, {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": False})

                # Read raw bytes and decode robustly (handles partial multibyte sequences)
                with open(p, "rb") as f:
                    chunk = f.read(read_n)
                decoder = codecs.getincrementaldecoder(enc)(errors="replace")
                text = decoder.decode(chunk, final=True)

                bytes_returned = len(chunk)
                truncated = bytes_returned < total

                payload = {
                    "ok": True,
                    "path": str(p.relative_to(WORK_DIR)),
                    "encoding": enc,
                    "encoding_used": enc,
                    "total_bytes": total,
                    "bytes_returned": bytes_returned,
                    "requested_mode": requested_mode,             # "full" or "sample"
                    "requested_max_bytes": None if requested_mode == "full" else max_bytes_arg,
                    "cap_bytes": cap,
                    "cap_enforced": cap_enforced,
                    "truncated": truncated,
                    "sample_range": {"start": 0, "end": bytes_returned},
                    "next_offset_bytes": bytes_returned if truncated else None,
                    "policy": {"hard_cap_full": FS_READ_HARD_CAP_FULL},
                    "content": text,
                }
                return _jr_ok(rpc_id, {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": False})
            except Exception as e:
                err = {"ok": False, "error": str(e), "cwd": str(WORK_DIR)}
                return _jr_ok(rpc_id, {"content": [{"type": "text", "text": json.dumps(err, indent=2)}], "isError": True})

        if name == "fs.listdir":
            listing = {"cwd": str(WORK_DIR), "ls": _dir_listing()}
            return _jr_ok(rpc_id,
                          {"content": [{"type": "text", "text": json.dumps(listing, indent=2)}], "isError": False})

        if name == "python.exec_bundle":
            files: dict = args.get("files") or {}
            entrypoint = args.get("entrypoint") or "main.py"
            reqs = args.get("requires") or []
            try:
                manifest = []
                for rel, content in files.items():
                    p = _safe_rel_path(rel)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                    manifest.append(_mk_artifact(p))
                missing, suggestions = _validate_requires(reqs)
                if missing:
                    err = {
                        "error": "MissingRequiredFiles",
                        "missing": missing,
                        "suggestions": suggestions,
                        "cwd": str(WORK_DIR),
                        "entrypoint": entrypoint
                    }
                    err = {"error": str(e), "cwd": str(WORK_DIR)}
                    return _jr_ok(rpc_id,
                                  {"content": [{"type": "text", "text": json.dumps(err, indent=2)}], "isError": True})
                rc, out, errtxt = _run_python(
                    f"# exec_bundle entrypoint\n# {entrypoint!r} will be executed below via -I\n" +
                    _safe_rel_path(entrypoint).read_text(encoding="utf-8"),
                    int(args.get("timeout_s", DEFAULT_TIMEOUT_S)),
                    int(args.get("memory_mb", DEFAULT_MEMORY_MB)),
                    stream=False,
                    filename=entrypoint
                )
                content = (out or "") + (f"\n[STDERR]\n{errtxt}" if (errtxt or "").strip() else "")
                return _jr_ok(rpc_id, {"content": [{"type": "text", "text": content}],
                                       "isError": rc != 0})
            except Exception as e:
                err = {"ok": False, "error": str(e), "cwd": str(WORK_DIR)}
                return _jr_ok(rpc_id,
                              {"content": [{"type": "text", "text": json.dumps(err, indent=2)}], "isError": True})

        if name == "python_eval":
            code = args.get("code", "")
            filename = args.get("filename") or args.get("save_as")  # alias
            timeout_s = int(args.get("timeout_s", DEFAULT_TIMEOUT_S))
            memory_mb = int(args.get("memory_mb", DEFAULT_MEMORY_MB))
            missing, suggestions = _validate_requires(args.get("requires"))
            if missing:
                err = {
                    "error": "MissingRequiredFiles",
                    "missing": missing,
                    "suggestions": suggestions,
                    "cwd": str(WORK_DIR),
                    "entrypoint": filename or "user_code.py"
                }
                # Structured error + snapshot
                return _jr_ok(rpc_id,
                              {"content": [{"type": "text", "text": json.dumps(err, indent=2)}], "isError": True})

            rc, out, err = _run_python(code, timeout_s, memory_mb, stream=False, filename=filename)
            entry = _safe_rel_path(filename)
            text = (out or "") + (f"\n[STDERR]\n{err}" if (err or "").strip() else "")
            return _jr_ok(rpc_id, {"content": [{"type": "text", "text": text}], "isError": rc != 0})

    return _jr_err(rpc_id, code=-32601, message=f"Method not found: {method}")

# -------------------- MCP over HTTP (POST /) --------------------
@app.post("/")
async def mcp_root(request: Request):
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8")) if body else {}
    except Exception as e:
        return _json_response(_jr_err(None, code=-32700, message=f"Parse error: {e}"), status_code=400)

    env = await mcp_root_jsonrpc_dict(data)
    if "__notification__" in env:
        return Response(status_code=204)  # zero-body per spec
    return _json_response(env, status_code=200)

# -------------------- MCP over HTTP+SSE (GET /sse + POST /message) --------------------
def _sse_headers():
    return {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}

@app.get("/sse")
async def sse_route(request: Request):
    async def _gen():
        sess = uuid.uuid4().hex
        SESSIONS.add(sess)
        q: asyncio.Queue[dict] = asyncio.Queue()
        SESSION_QUEUES[sess] = q
        try:
            base = str(request.base_url).rstrip("/")
            endpoint_url = f"{base}/message?sessionId={sess}"
            # REQUIRED first event: endpoint
            yield _sse_event(endpoint_url, event="endpoint")

            # Stream loop: deliver queued JSON-RPC messages as event: message
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield _sse_event(msg, event="message")
                except asyncio.TimeoutError:
                    # heartbeat as comment (ignored by clients)
                    yield b": ping\n\n"
                if await request.is_disconnected():
                    break
        finally:
            SESSIONS.discard(sess)
            SESSION_QUEUES.pop(sess, None)

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_sse_headers())

@app.post("/message")
async def message_alias(request: Request, sessionId: Optional[str] = Query(default=None)):
    # Optionally enforce sessions:
    # if not sessionId or sessionId not in SESSION_QUEUES:
    #     return Response(status_code=400)
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8")) if body else {}
    except Exception as e:
        env = _jr_err(None, code=-32700, message=f"Parse error: {e}")
        if sessionId and sessionId in SESSION_QUEUES:
            await SESSION_QUEUES[sessionId].put(env)
        return Response(status_code=200)

    env = await mcp_root_jsonrpc_dict(data)
    if "__notification__" not in env:
        if sessionId and sessionId in SESSION_QUEUES:
            await SESSION_QUEUES[sessionId].put(env)
    # HTTP body is ignored by clients; 200 ack is fine.
    return Response(status_code=200)

# -------------------- Simple /eval (OpenWebUI external tool) --------------------
# --- REPLACE EvalRequest with: ---
class EvalRequest(BaseModel):
    code: str
    filename: Optional[str] = None
    requires: Optional[list[str]] = None
    timeout_s: Optional[int] = DEFAULT_TIMEOUT_S
    memory_mb: Optional[int] = DEFAULT_MEMORY_MB

# --- UPDATE /eval handler: ---
@app.post("/eval", response_model=EvalResponse)
async def eval_code(req: EvalRequest) -> EvalResponse:
    missing, suggestions = _validate_requires(req.requires)
    if missing:
        # return a helpful stderr that tools can parse
        msg = json.dumps({"error": "MissingRequiredFiles", "missing": missing, "suggestions": suggestions, "cwd": str(WORK_DIR), "ls": _dir_listing()})
        return EvalResponse(stdout="", stderr=msg, returncode=2)

    rc, out, err = _run_python(req.code, int(req.timeout_s or DEFAULT_TIMEOUT_S), int(req.memory_mb or DEFAULT_MEMORY_MB), stream=False, filename=req.filename)
    return EvalResponse(stdout=out, stderr=err, returncode=rc)

# -------------------- OpenAI-compatible --------------------
@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": SERVICE_NAME, "object": "model", "owned_by": "local"}]}

class ChatReq(ChatCompletionsRequest): pass

def _extract_code_from_messages(messages: list[ChatMessage]) -> str:
    content = ""
    for m in reversed(messages):
        if m.role == "user":
            content = m.content or ""
            break
    if "```" in content:
        import re
        m = re.search(r"```(?:python)?\s*(.*?)```", content, flags=re.S)
        if m: return m.group(1)
    return content

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatReq, request: Request):
    if req.model != SERVICE_NAME:
        return JSONResponse(status_code=400, content={"error": {"message": "unknown model"}})

    code = _extract_code_from_messages(req.messages)
    timeout_s = int(req.timeout_s or DEFAULT_TIMEOUT_S)
    memory_mb = int(req.memory_mb or DEFAULT_MEMORY_MB)
    created = int(time.time())
    cmpl_id = f"chatcmpl-{SERVICE_NAME}-{created}"

    if not req.stream:
        rc, out, err = _run_python(code, timeout_s, memory_mb, stream=False)  # type: ignore
        content = (out or "") + (f"\n[STDERR]\n{err}" if (err or "").strip() else "")
        return {
            "id": cmpl_id,
            "object": "chat.completion",
            "created": created,
            "model": SERVICE_NAME,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop" if rc == 0 else "error"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def _stream():
        yield _sse_event({
            "id": cmpl_id, "object": "chat.completion.chunk", "created": created, "model": SERVICE_NAME,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        })
        itr = _run_python(code, timeout_s, memory_mb, stream=True)  # type: ignore
        for chunk in itr:
            if await request.is_disconnected(): break
            yield _sse_event({
                "id": cmpl_id, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": SERVICE_NAME, "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]
            })
        yield _sse_event({
            "id": cmpl_id, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": SERVICE_NAME, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        })
        yield b"data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

# -------------------- Main --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, port=8013)
