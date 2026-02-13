import os, json, logging, time, uuid, asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

# Optional: Kubernetes client for tool execution
try:
    from kubernetes import client as k8s_client, config as k8s_config
    HAVE_K8S = True
except Exception:
    HAVE_K8S = False

# Server-side LlamaStack tool invocation via HTTP
HAVE_LS = True  # Assume reachable via UPSTREAM; we will handle errors at runtime

UPSTREAM = os.environ.get("UPSTREAM_BASE", "http://ollama-gpt-oss-120b.gpt-oss.svc.cluster.local:11434").rstrip("/")
BACKEND = os.environ.get("PROXY_BACKEND", "ollama").strip().lower()
TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "60"))
READ_TIMEOUT = float(os.environ.get("PROXY_READ_TIMEOUT", str(max(120, int(TIMEOUT)))))
DEFAULT_MODEL = os.environ.get("PROXY_DEFAULT_MODEL", "gpt-oss:120b")
DEFAULT_MAX_TOKENS = int(os.environ.get("PROXY_DEFAULT_MAX_TOKENS", "256"))
MAX_TOKENS_CAP = int(os.environ.get("PROXY_MAX_TOKENS_CAP", "120"))
# Weather config (fallback via Open-Meteo APIs)
WEATHER_UNITS = os.environ.get("WEATHER_UNITS", "metric")  # metric|imperial
OPEN_METEO_GEOCODE = os.environ.get("OPEN_METEO_GEOCODE", "https://geocoding-api.open-meteo.com/v1/search")
OPEN_METEO_FORECAST = os.environ.get("OPEN_METEO_FORECAST", "https://api.open-meteo.com/v1/forecast")
# Web search (Tavily) config (proxy-side fallback; disabled by default to rely on LlamaStack providers)
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_ENDPOINT = os.environ.get("TAVILY_ENDPOINT", "https://api.tavily.com/search")
WEB_FALLBACK_ENABLED = os.environ.get("PROXY_WEB_FALLBACK_ENABLED", "false").lower() == "true"
# Claude Code agent
CLAUDE_CODE_URL = os.environ.get("CLAUDE_CODE_URL", "http://claude-code-agent.home-assistant.svc.cluster.local:8010")
# Home Assistant API (for TTS push notifications)
HA_BASE = os.environ.get("HA_BASE", "http://home-assistant.home-assistant.svc.cluster.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_TTS_ENTITY = os.environ.get("TTS_ENTITY_ID", "tts.openai_tts_kokoro")
HA_MEDIA_PLAYER = os.environ.get("HA_MEDIA_PLAYER", "media_player.home_assistant_voice_0a58e3_media_player")
# MinIO object storage
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://minio.home-assistant.svc.cluster.local:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ROOT_USER", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_ROOT_PASSWORD", "")
MINIO_DEFAULT_BUCKET = os.environ.get("MINIO_DEFAULT_BUCKET", "claude-results")
MINIO_SECURE = MINIO_ENDPOINT.startswith("https")

try:
    from minio import Minio
    from io import BytesIO
    HAVE_MINIO = True
except ImportError:
    HAVE_MINIO = False

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
log = logging.getLogger("openai-proxy")

app = FastAPI(title="OpenAI Responses->Chat Completions Proxy")

_models_cache = None
_llama_client = None

async def llamastack_invoke_tool_http(client: httpx.AsyncClient, name: str, args: dict|None):
    """Invoke a tool on the LlamaStack server via HTTP (uses server-configured providers/keys)."""
    try:
        url = f"{UPSTREAM}/v1/tool-runtime/invoke"
        payload = {"tool_name": name, "kwargs": args or {}}
        r = await client.post(url, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        # Expected shape: { content: <json-string or dict>, error_message, error_code, metadata }
        j = r.json()
        return {"source": "llamastack", "tool": name, "result": j}
    except Exception as e:
        return {"error": f"llamastack tool invoke failed: {e}"}


def _extract_messages_from_input(_input) -> list:
    messages = []
    if isinstance(_input, list):
        for part in _input:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "message":
                    role = part.get("role", "user")
                    if role == "developer":
                        role = "system"
                    content = part.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        texts = []
                        for seg in content:
                            if isinstance(seg, dict) and seg.get("type") in ("input_text", "text"):
                                texts.append(seg.get("text") or seg.get("input_text") or "")
                            elif isinstance(seg, str):
                                texts.append(seg)
                        text = "\n".join([t for t in texts if t])
                    messages.append({"role": role, "content": text})
                elif ptype in ("input_text", "text"):
                    text = part.get("text") or part.get("input_text") or ""
                    if text:
                        messages.append({"role": "user", "content": text})
            elif isinstance(part, str):
                messages.append({"role": "user", "content": part})
    elif isinstance(_input, str):
        messages.append({"role": "user", "content": _input})
    return messages

async def post_json(client: httpx.AsyncClient, path: str, data: dict):
    url = f"{UPSTREAM}{path}"
    r = await client.post(url, json=data, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

async def get_json(client: httpx.AsyncClient, path: str):
    url = f"{UPSTREAM}{path}"
    r = await client.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def _is_llamastack() -> bool:
    return BACKEND == "llamastack"

# Namespace aliasing: PROXY_NS_ALIASES like "minecraft:minecraft;prod:production"
_aliases = None

def _parse_ns_aliases():
    global _aliases
    if _aliases is not None:
        return _aliases
    raw = os.environ.get("PROXY_NS_ALIASES", "minecraft:minecraft")
    out = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        out.append((k.strip().lower(), v.strip()))
    _aliases = out
    return _aliases

def _detect_default_namespace(messages: list) -> str | None:
    # Only check the LATEST user message to avoid picking up namespaces from conversation history
    latest_user_text = ""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            latest_user_text = m["content"].lower()
            break
    if not latest_user_text:
        return None
    for kw, ns in _parse_ns_aliases():
        if kw in latest_user_text:
            return ns
    return None

_k8s_inited = False
v1 = None
custom = None

def _init_k8s():
    global _k8s_inited, v1, custom
    if _k8s_inited or not HAVE_K8S:
        return
    try:
        k8s_config.load_incluster_config()
    except Exception:
        try:
            k8s_config.load_kube_config()
        except Exception as e:
            log.warning("K8s config load failed: %s", e)
            return
    v1 = k8s_client.CoreV1Api()
    custom = k8s_client.CustomObjectsApi()
    _k8s_inited = True

async def k8s_pods_list_in_namespace(ns: str, label: str|None):
    _init_k8s()
    if not v1:
        return {"error":"k8s client not available"}
    ret = v1.list_namespaced_pod(ns, label_selector=label or None)
    items = []
    for p in ret.items:
        ready = 0; total = 0
        for cs in p.status.container_statuses or []:
            total += 1
            if cs.ready:
                ready += 1
        phase = p.status.phase
        conds = {c.type:c.status for c in (p.status.conditions or [])}
        items.append({"name": p.metadata.name, "namespace": ns, "phase": phase, "ready": f"{ready}/{total}", "conditions": conds})
    return {"namespace": ns, "count": len(items), "pods": items}

_SYSTEM_NS_PREFIXES = ("openshift-", "kube-", "open-cluster-", "hive", "multicluster", "assisted-", "rhacs-")

async def k8s_pods_list(ns: str|None, label: str|None):
    _init_k8s()
    if not v1:
        return {"error":"k8s client not available"}
    items = []
    if ns:
        ret = v1.list_namespaced_pod(ns, label_selector=label or None)
        pods = ret.items
    else:
        ret = v1.list_pod_for_all_namespaces(label_selector=label or None)
        # Filter out system namespaces for cluster-wide queries to keep payload manageable
        pods = [p for p in ret.items if not any(p.metadata.namespace.startswith(pfx) for pfx in _SYSTEM_NS_PREFIXES)]
    for p in pods:
        ready = 0; total = 0
        for cs in p.status.container_statuses or []:
            total += 1
            if cs.ready:
                ready += 1
        phase = p.status.phase
        conds = {c.type:c.status for c in (p.status.conditions or [])}
        items.append({"name": p.metadata.name, "namespace": p.metadata.namespace, "phase": phase, "ready": f"{ready}/{total}", "conditions": conds})
    return {"count": len(items), "pods": items}

async def k8s_pods_get(name: str, ns: str|None):
    _init_k8s()
    if not v1:
        return {"error":"k8s client not available"}
    if ns:
        p = v1.read_namespaced_pod(name=name, namespace=ns)
    else:
        # search all namespaces (first match)
        plist = v1.list_pod_for_all_namespaces(field_selector=f"metadata.name={name}")
        if not plist.items:
            return {"error": f"pod {name} not found"}
        p = plist.items[0]
    conds = {c.type:c.status for c in (p.status.conditions or [])}
    containers = []
    for cs in p.status.container_statuses or []:
        containers.append({"name": cs.name, "ready": cs.ready, "restartCount": cs.restart_count, "state": list(cs.state.to_dict().keys())[0] if cs.state else None})
    return {"name": p.metadata.name, "namespace": p.metadata.namespace, "phase": p.status.phase, "conditions": conds, "containers": containers}

async def k8s_pods_log(name: str, ns: str|None, container: str|None, tail: int|None):
    _init_k8s()
    if not v1:
        return {"error":"k8s client not available"}
    if not ns:
        # try global search for namespace
        plist = v1.list_pod_for_all_namespaces(field_selector=f"metadata.name={name}")
        if not plist.items:
            return {"error": f"pod {name} not found"}
        ns = plist.items[0].metadata.namespace
    logtxt = v1.read_namespaced_pod_log(name=name, namespace=ns, container=container or None, tail_lines=tail or 100)
    return {"name": name, "namespace": ns, "container": container, "tail": tail or 100, "log": logtxt[-4000:]}  # limit size

async def k8s_pods_top(ns: str|None, name: str|None, all_namespaces: bool|None, label_selector: str|None):
    _init_k8s()
    if not custom:
        return {"error":"metrics.k8s.io not available"}
    group, version = "metrics.k8s.io", "v1beta1"
    try:
        if name and ns:
            obj = custom.get_namespaced_custom_object(group, version, ns, "pods", name)
            items = [obj]
        elif ns:
            obj = custom.list_namespaced_custom_object(group, version, ns, "pods", label_selector=label_selector or None)
            items = obj.get("items", [])
        else:
            obj = custom.list_cluster_custom_object(group, version, "pods")
            items = obj.get("items", [])
    except Exception as e:
        return {"error": f"metrics query failed: {e}"}
    out = []
    for it in items:
        nm = it["metadata"]["name"]; nns = it["metadata"]["namespace"]
        for c in it.get("containers", []):
            cpu = c["usage"].get("cpu","0")
            mem = c["usage"].get("memory","0")
            out.append({"pod": nm, "namespace": nns, "container": c["name"], "cpu": cpu, "memory": mem})
    return {"metrics": out}

async def k8s_cluster_operators():
    """Query OpenShift ClusterOperator status (config.openshift.io/v1)."""
    _init_k8s()
    if not custom:
        return {"error": "k8s client not available"}
    try:
        obj = custom.list_cluster_custom_object("config.openshift.io", "v1", "clusteroperators")
    except Exception as e:
        return {"error": f"ClusterOperator query failed: {e}"}
    operators = []
    for co in obj.get("items", []):
        name = co["metadata"]["name"]
        conditions = {}
        for c in co.get("status", {}).get("conditions", []):
            conditions[c["type"]] = {"status": c["status"], "reason": c.get("reason", ""), "message": c.get("message", "")}
        avail = conditions.get("Available", {}).get("status") == "True"
        degraded = conditions.get("Degraded", {}).get("status") == "True"
        progressing = conditions.get("Progressing", {}).get("status") == "True"
        operators.append({
            "name": name,
            "available": avail,
            "degraded": degraded,
            "progressing": progressing,
            "degraded_reason": conditions.get("Degraded", {}).get("reason", "") if degraded else "",
            "degraded_message": conditions.get("Degraded", {}).get("message", "")[:200] if degraded else "",
        })
    total = len(operators)
    avail_count = sum(1 for o in operators if o["available"])
    degraded_count = sum(1 for o in operators if o["degraded"])
    progressing_count = sum(1 for o in operators if o["progressing"])
    return {
        "total": total,
        "available": avail_count,
        "degraded": degraded_count,
        "progressing": progressing_count,
        "operators": operators,
    }

def _summarize_cluster_operators(res: dict) -> str:
    if "error" in res:
        return f"Could not query cluster operators: {res['error']}"
    total = res.get("total", 0)
    avail = res.get("available", 0)
    degraded = res.get("degraded", 0)
    progressing = res.get("progressing", 0)
    text = f"OpenShift cluster has {total} operators: {avail} available"
    if degraded:
        text += f", {degraded} degraded"
        bad = [o for o in res.get("operators", []) if o.get("degraded")]
        names = ", ".join(o["name"] for o in bad[:5])
        text += f" ({names})"
    if progressing:
        text += f", {progressing} progressing"
    if not degraded and avail == total:
        text += ". Cluster is healthy."
    else:
        text += "."
    return text

# ---------- Claude Code agent helpers ----------
async def claude_code_submit(client: httpx.AsyncClient, prompt: str, notify: bool = True):
    """Submit a task to the Claude Code agent and start a completion notifier."""
    try:
        r = await client.post(f"{CLAUDE_CODE_URL}/tasks", json={"prompt": prompt}, timeout=15)
        r.raise_for_status()
        data = r.json()
        task_id = data.get("task_id")
        if task_id and notify:
            asyncio.get_event_loop().create_task(_claude_completion_notifier(task_id, prompt[:80]))
        return data
    except Exception as e:
        log.warning("claude_code_submit failed: %s", e)
        return {"error": f"Failed to submit Claude Code task: {e}"}

async def claude_code_status(client: httpx.AsyncClient, task_id: str | None = None):
    """Get status of Claude Code task(s)."""
    # Sanitize placeholder task IDs from the model
    if task_id and (task_id.startswith("<") or task_id in ("task_id", "id", "unknown", "None", "null")):
        task_id = None
    try:
        if task_id:
            r = await client.get(f"{CLAUDE_CODE_URL}/tasks/{task_id}", timeout=10)
        else:
            r = await client.get(f"{CLAUDE_CODE_URL}/tasks", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("claude_code_status failed: %s", e)
        return {"error": f"Failed to get Claude Code status: {e}"}

def _summarize_claude_code_tasks(data) -> str:
    """Build a voice-friendly summary of Claude Code task status."""
    if isinstance(data, dict) and "error" in data:
        return f"Could not reach Claude Code agent: {data['error']}"
    if isinstance(data, dict) and "task_id" in data and "status" in data and "id" not in data:
        # Submit response: {"task_id": "xxx", "status": "queued"}
        return f"I've submitted that task to Claude Code. Task ID is {data['task_id']}, status: {data['status']}. You can ask me for the status anytime."
    if isinstance(data, dict) and ("id" in data or "task_id" in data):
        # Single task detail
        task_id = data.get("id") or data.get("task_id")
        s = data.get("status", "unknown")
        prompt = (data.get("prompt") or "")[:80]
        text = f"Claude Code task {task_id}: {s}."
        if s == "completed" and data.get("result"):
            res = data["result"]
            if isinstance(res, dict):
                res = res.get("result") or res.get("content") or json.dumps(res)[:200]
            text += f" Result: {str(res)[:200]}"
        elif s == "failed" and data.get("error"):
            text += f" Error: {str(data['error'])[:150]}"
        elif s in ("queued", "running"):
            text += f" Prompt: {prompt}"
        return text
    if isinstance(data, list):
        if not data:
            return "No Claude Code tasks have been submitted yet."
        latest = data[-1]
        total = len(data)
        s = latest.get("status", "unknown")
        text = f"There are {total} Claude Code tasks. The latest (task {latest['id']}) is {s}."
        running = [t for t in data if t.get("status") == "running"]
        if running:
            text = f"Claude Code is currently running task {running[0]['id']}. {total} total tasks."
        return text
    return "No Claude Code task information available."

# ---------- MinIO helpers ----------
_minio_client = None

def _get_minio_client():
    global _minio_client
    if _minio_client is not None:
        return _minio_client
    if not HAVE_MINIO or not MINIO_SECRET_KEY:
        return None
    endpoint = MINIO_ENDPOINT.replace("https://", "").replace("http://", "")
    _minio_client = Minio(endpoint, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)
    return _minio_client

def _ensure_minio_bucket(bucket: str = None):
    mc = _get_minio_client()
    if not mc:
        return
    bucket = bucket or MINIO_DEFAULT_BUCKET
    try:
        if not mc.bucket_exists(bucket):
            mc.make_bucket(bucket)
            log.info("Created MinIO bucket: %s", bucket)
    except Exception as e:
        log.warning("MinIO bucket check/create failed: %s", e)

async def minio_upload(content: str, object_name: str, bucket: str = None) -> dict:
    mc = _get_minio_client()
    if not mc:
        return {"error": "MinIO client not available"}
    bucket = bucket or MINIO_DEFAULT_BUCKET
    try:
        _ensure_minio_bucket(bucket)
        data = content.encode("utf-8")
        mc.put_object(bucket, object_name, BytesIO(data), length=len(data), content_type="text/plain")
        log.info("Uploaded to MinIO: %s/%s (%d bytes)", bucket, object_name, len(data))
        return {"bucket": bucket, "object_name": object_name, "size_bytes": len(data), "endpoint": MINIO_ENDPOINT}
    except Exception as e:
        log.warning("MinIO upload failed: %s", e)
        return {"error": f"MinIO upload failed: {e}"}

# ---------- Claude Code -> MinIO background worker ----------
_minio_pending_tasks: dict[str, dict] = {}

async def _claude_to_minio_worker(task_id: str, bucket: str, object_prefix: str):
    poll_interval = 5
    max_polls = 360  # 30 minutes
    log.info("minio-worker: watching task %s for upload to %s/%s", task_id, bucket, object_prefix)
    async with httpx.AsyncClient() as client:
        for _ in range(max_polls):
            await asyncio.sleep(poll_interval)
            try:
                r = await client.get(f"{CLAUDE_CODE_URL}/tasks/{task_id}", timeout=10)
                r.raise_for_status()
                task_data = r.json()
            except Exception as e:
                log.warning("minio-worker: poll failed for task %s: %s", task_id, e)
                continue
            status = task_data.get("status")
            if status in ("completed", "failed"):
                result = task_data.get("result")
                if result is None:
                    result = task_data.get("error") or "no output"
                if isinstance(result, dict):
                    content = json.dumps(result, indent=2)
                    ext = ".json"
                else:
                    content = str(result)
                    ext = ".txt"
                object_name = f"{object_prefix}/{task_id}{ext}"
                upload_result = await minio_upload(content, object_name, bucket)
                if "error" in upload_result:
                    log.warning("minio-worker: upload failed for task %s: %s", task_id, upload_result["error"])
                    await ha_tts_speak(f"Your Claude Code task has finished, but the upload to MinIO failed.")
                else:
                    log.info("minio-worker: task %s result uploaded to %s/%s", task_id, bucket, object_name)
                    await ha_tts_speak(f"Your Claude Code task has finished and the results have been uploaded to the {bucket} bucket in MinIO.")
                _minio_pending_tasks[task_id]["upload"] = upload_result
                _minio_pending_tasks[task_id]["status"] = "uploaded" if "error" not in upload_result else "upload_failed"
                return
            elif status == "cancelled":
                log.info("minio-worker: task %s was cancelled, skipping upload", task_id)
                _minio_pending_tasks[task_id]["status"] = "cancelled"
                return
    log.warning("minio-worker: task %s timed out after polling", task_id)
    _minio_pending_tasks[task_id]["status"] = "poll_timeout"

async def claude_code_submit_to_minio(client: httpx.AsyncClient, prompt: str, bucket: str = None, object_prefix: str = "claude-tasks"):
    submit_result = await claude_code_submit(client, prompt, notify=False)
    if "error" in submit_result:
        return submit_result
    task_id = submit_result.get("task_id", "unknown")
    bucket = bucket or MINIO_DEFAULT_BUCKET
    _minio_pending_tasks[task_id] = {"task_id": task_id, "bucket": bucket, "object_prefix": object_prefix, "status": "polling"}
    asyncio.get_event_loop().create_task(_claude_to_minio_worker(task_id, bucket, object_prefix))
    return {
        "task_id": task_id,
        "status": submit_result.get("status", "queued"),
        "minio_bucket": bucket,
        "minio_path": f"{object_prefix}/{task_id}",
        "message": "Task submitted. Results will be uploaded to MinIO when complete.",
    }

# ---------- Claude Code completion notifier (non-MinIO) ----------
async def _claude_completion_notifier(task_id: str, prompt_summary: str):
    """Poll a Claude Code task and speak a notification when it finishes."""
    poll_interval = 5
    max_polls = 360  # 30 minutes
    log.info("notify-worker: watching task %s for completion", task_id)
    async with httpx.AsyncClient() as client:
        for _ in range(max_polls):
            await asyncio.sleep(poll_interval)
            try:
                r = await client.get(f"{CLAUDE_CODE_URL}/tasks/{task_id}", timeout=10)
                r.raise_for_status()
                task_data = r.json()
            except Exception:
                continue
            status = task_data.get("status")
            if status == "completed":
                result = task_data.get("result")
                # Build a brief spoken summary
                summary = ""
                if isinstance(result, dict):
                    summary = result.get("result", "") or result.get("content", "")
                elif isinstance(result, str):
                    summary = result
                summary = summary[:200] if summary else "no details available"
                await ha_tts_speak(f"Your Claude Code task has finished. {summary}")
                return
            elif status == "failed":
                error = task_data.get("error", "unknown error")
                await ha_tts_speak(f"Your Claude Code task has failed. {str(error)[:150]}")
                return
            elif status == "cancelled":
                return
    log.warning("notify-worker: task %s timed out", task_id)

@app.on_event("startup")
async def _startup_minio():
    _ensure_minio_bucket()

# ---------- HA TTS push notification ----------
async def ha_tts_speak(message: str):
    """Push a spoken notification to the voice assistant via HA TTS service."""
    if not HA_TOKEN or HA_TOKEN.startswith("REPLACE"):
        log.warning("ha_tts_speak: no HA_TOKEN configured, skipping notification")
        return
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{HA_BASE}/api/services/tts/speak",
                headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
                json={
                    "entity_id": HA_TTS_ENTITY,
                    "media_player_entity_id": HA_MEDIA_PLAYER,
                    "message": message,
                },
                timeout=30,
            )
            log.info("ha_tts_speak: status=%d message=%r", r.status_code, message[:120])
    except Exception as e:
        log.warning("ha_tts_speak failed: %s", e)

# ---------- Weather helpers ----------
async def _geocode_city(client: httpx.AsyncClient, city: str):
    try:
        r = await client.get(OPEN_METEO_GEOCODE, params={"name": city, "count": 1})
        r.raise_for_status()
        j = r.json()
        if j.get("results"):
            res = j["results"][0]
            return {"latitude": res.get("latitude"), "longitude": res.get("longitude"), "name": res.get("name"), "country": res.get("country")}
    except Exception as e:
        log.warning("geocode failed: %s", e)
    return None

async def weather_get_current(client: httpx.AsyncClient, city: str|None, lat: float|None, lon: float|None, units: str|None):
    # Always use the configured WEATHER_UNITS (ignore model's preference)
    units = WEATHER_UNITS.lower()
    if (lat is None or lon is None) and city:
        geo = await _geocode_city(client, city)
        if geo:
            lat = geo.get("latitude"); lon = geo.get("longitude")
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ["temperature_2m","wind_speed_10m","weather_code"],
    }
    if units == "imperial":
        params["temperature_unit"] = "fahrenheit"
        params["wind_speed_unit"] = "mph"
    try:
        r = await client.get(OPEN_METEO_FORECAST, params=params)
        r.raise_for_status()
        j = r.json()
        cur = j.get("current") or {}
        # Make units explicit in the result so the LLM doesn't guess wrong
        temp = cur.get("temperature_2m")
        wind = cur.get("wind_speed_10m")
        weather_code = cur.get("weather_code")
        if units == "imperial":
            return {"source":"open-meteo", "city": city, "temperature": f"{temp} degrees Fahrenheit", "wind_speed": f"{wind} miles per hour", "weather_code": weather_code}
        else:
            return {"source":"open-meteo", "city": city, "temperature": f"{temp} degrees Celsius", "wind_speed": f"{wind} km/h", "weather_code": weather_code}
    except Exception as e:
        return {"error": f"weather forecast failed: {e}"}

# ---------- Web search (Tavily) ----------
async def web_search_tavily(client: httpx.AsyncClient, query: str, max_results: int|None = 5):
    if not TAVILY_API_KEY:
        return {"error":"missing TAVILY_API_KEY"}
    try:
        r = await client.post(TAVILY_ENDPOINT, json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": max(1, int(max_results or 5))
        }, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        # Normalize to a compact structure
        items = []
        for it in j.get("results", [])[: max_results or 5]:
            items.append({
                "title": it.get("title"),
                "url": it.get("url"),
                "content": it.get("content")
            })
        return {"source":"tavily","results": items}
    except Exception as e:
        return {"error": f"tavily search failed: {e}"}
async def weather_get_forecast(client: httpx.AsyncClient, city: str|None, lat: float|None, lon: float|None, days: int|None, units: str|None):
    # Always use the configured WEATHER_UNITS (ignore model's preference)
    units = WEATHER_UNITS.lower()
    if (lat is None or lon is None) and city:
        geo = await _geocode_city(client, city)
        if geo:
            lat = geo.get("latitude"); lon = geo.get("longitude")
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ["temperature_2m_max","temperature_2m_min","weather_code","wind_speed_10m_max"],
        "forecast_days": max(1, min(int(days or 3), 7))
    }
    if units == "imperial":
        params["temperature_unit"] = "fahrenheit"
        params["wind_speed_unit"] = "mph"
    try:
        r = await client.get(OPEN_METEO_FORECAST, params=params)
        r.raise_for_status()
        j = r.json()
        return {"source":"open-meteo","units": units, "daily": j.get("daily")}
    except Exception as e:
        return {"error": f"weather forecast failed: {e}"}
def _summarize_pods_list(res: dict) -> str:
    pods = (res or {}).get("pods") or []
    ns = (res or {}).get("namespace") or "default"
    if not pods:
        return f"No pods found in namespace '{ns}'."
    lines = [f"Pods in {ns}:"]
    for p in pods[:10]:
        lines.append(f"- {p.get('name')}: phase={p.get('phase')} ready={p.get('ready')}")
    if len(pods) > 10:
        lines.append(f"(+{len(pods)-10} more)")
    return "\n".join(lines)


def _summarize_pods_overview(res: dict) -> str:
    pods = (res or {}).get("pods") or []
    if not pods:
        return "No pods found across namespaces."
    # Filter to user namespaces (skip system namespaces)
    user_pods = [p for p in pods if not any(p.get("namespace","").startswith(pfx) for pfx in _SYSTEM_NS_PREFIXES)]
    # Group by namespace
    ns_groups = {}
    non_running = []
    for p in user_pods:
        ns = p.get("namespace") or "default"
        phase = p.get("phase") or "Unknown"
        ns_groups.setdefault(ns, {"running": 0, "total": 0})
        ns_groups[ns]["total"] += 1
        if phase in ("Running", "Succeeded"):
            ns_groups[ns]["running"] += 1
        else:
            non_running.append(f"{ns}/{p.get('name')} ({phase})")
    lines = [f"Cluster overview: {len(user_pods)} user pods across {len(ns_groups)} namespaces."]
    for ns, counts in sorted(ns_groups.items()):
        lines.append(f"{ns}: {counts['running']}/{counts['total']} healthy")
    if non_running:
        lines.append("Issues: " + "; ".join(non_running[:5]))
    return " ".join(lines)

async def ensure_model(client: httpx.AsyncClient, model: str) -> str:
    global _models_cache
    try:
        if not _models_cache:
            if _is_llamastack():
                res = await get_json(client, "/v1/models")
                _models_cache = {m.get("identifier") or m.get("id") for m in res.get("data", [])}
            else:
                tags = await get_json(client, "/api/tags")
                _models_cache = {m.get("name") for m in tags.get("models", [])}
        if _is_llamastack() and model not in _models_cache:
            alt = f"ollama/{model}"
            if alt in _models_cache:
                return alt
        if model not in _models_cache:
            log.warning("Unknown model '%s'; falling back to '%s'", model, DEFAULT_MODEL)
            return f"ollama/{DEFAULT_MODEL}" if _is_llamastack() else DEFAULT_MODEL
        return model
    except Exception as e:
        log.warning("Model check failed (%s); using '%s'", e, DEFAULT_MODEL)
        return DEFAULT_MODEL

@app.post("/v1/responses")
async def responses(req: Request):
    body = await req.json()
    model = body.get("model")
    want_stream = bool(body.get("stream")) or ("text/event-stream" in (req.headers.get("accept") or ""))
    # Convert Responses API into Chat Completions API
    messages = []
    if "messages" in body and isinstance(body["messages"], list):
        messages = body["messages"]
    else:
        # Map Responses 'input' array into chat messages
        _input = body.get("input")
        messages = _extract_messages_from_input(_input)
        # Ensure at least one message (avoid empty prompt)
        if not messages:
            content = _input if isinstance(_input, str) else ""
            messages = [{"role": "user", "content": content}]

    # Extract generation controls
    max_tokens = None
    try:
        max_tokens = int(body.get("max_output_tokens") or body.get("max_tokens") or 0)
    except Exception:
        max_tokens = None
    if not max_tokens or max_tokens <= 0:
        max_tokens = DEFAULT_MAX_TOKENS
    # Enforce an upper cap to avoid long generations that time out downstream (TTS/device)
    max_tokens = min(max_tokens, MAX_TOKENS_CAP)
    temperature = body.get("temperature")
    top_p = body.get("top_p")

    if want_stream:
        async def sse_gen():
            response_id = f"resp_{uuid.uuid4().hex[:8]}"
            item_id = f"msg_{uuid.uuid4().hex[:8]}"
            seq = 0

            def ev(name: str, data: dict):
                nonlocal seq
                seq += 1
                # Ensure type and sequence_number present
                data.setdefault("type", name)
                data.setdefault("sequence_number", seq)
                return f"event: {name}\n" + f"data: {json.dumps(data)}\n\n"

            # Created event
            created_evt = {
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created": int(time.time()),
                    "model": model or DEFAULT_MODEL,
                    "output": [],
                }
            }
            yield ev("response.created", created_evt)

            timeout = httpx.Timeout(connect=TIMEOUT, read=READ_TIMEOUT, write=TIMEOUT, pool=TIMEOUT)
            async with httpx.AsyncClient(timeout=timeout) as client:
                if _is_llamastack():
                    # Fetch tools once and attach
                    tools_res = await get_json(client, "/v1/tools")
                    tools = []
                    for t in tools_res.get("data", []):
                        # Advertise all server-side tools from LlamaStack; execution is routed appropriately.
                        tools.append({"type":"function","function":{"name": t.get("name"), "parameters": t.get("input_schema") or {"type":"object"}}})
                    # Add Claude Code tools (proxy-side, not in LlamaStack)
                    tools.append({"type":"function","function":{"name":"claude_code_submit","description":"Submit a coding task to the Claude Code agent running in the cluster. Use when the user asks Claude Code to do something.","parameters":{"type":"object","properties":{"prompt":{"type":"string","description":"The task description for Claude Code"}},"required":["prompt"]}}})
                    tools.append({"type":"function","function":{"name":"claude_code_status","description":"Check the status of Claude Code tasks. Call with no arguments to list all tasks, or with a task_id to get details on a specific task.","parameters":{"type":"object","properties":{"task_id":{"type":"string","description":"Optional task ID to check. Omit to list all tasks."}}}}})
                    tools.append({"type":"function","function":{"name":"claude_code_submit_to_minio","description":"Submit a coding task to Claude Code and automatically upload the result to MinIO object storage when complete. Use when the user wants Claude Code output stored in MinIO.","parameters":{"type":"object","properties":{"prompt":{"type":"string","description":"The task description for Claude Code"},"bucket":{"type":"string","description":"MinIO bucket name (default: claude-results)"},"object_prefix":{"type":"string","description":"Path prefix in the bucket (default: claude-tasks)"}},"required":["prompt"]}}})
                    # Add a gentle system hint about tool usage and voice-optimized responses
                    sys_hint = {"role":"system","content":"You are a helpful voice assistant. Keep responses brief and conversational - aim for 1-3 sentences unless more detail is explicitly requested. Be direct and get to the point quickly. Avoid unnecessary preamble, lists, or markdown formatting unless specifically asked. Since your responses will be spoken aloud, always spell out units fully (say 'degrees Fahrenheit' not '°F', 'miles per hour' not 'mph', etc.). You have Kubernetes tools (pods_list_in_namespace, pods_get, pods_log, pods_top), Weather tools (get_current_weather, get_weather_forecast), Web Search (web_search), Claude Code tools (claude_code_submit to give Claude Code a task, claude_code_status to check task progress, claude_code_submit_to_minio to submit a task and automatically upload results to MinIO object storage). Use them to answer cluster, weather, web, and coding questions. Do not say you lack access; instead, call a tool. You understand multiple languages. If you receive text in a non-English language (Arabic, French, Spanish, etc.), translate it to English and summarize the content. IMPORTANT: The speech-to-text system may automatically translate non-English audio (such as Arabic, French, Spanish, etc.) into English during transcription. If the user asks you to translate or listen to something in another language and the transcribed text is already in English, do NOT say 'this is already in English' — instead, simply summarize and present the content. The translation has already been done for you by the STT system. Always respond in English unless explicitly asked to respond in another language."}
                    msg_with_hint = [sys_hint] + messages
                    # Decide tool_choice based ONLY on the latest user message (avoid triggering on assistant/system text)
                    user_text = ""
                    for m in reversed(messages):
                        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
                            user_text = m.get("content") or ""
                            break
                    user_low = user_text.lower()
                    k8s_like_user = any(w in user_low for w in ["kubernetes","namespace","pod","deployment","statefulset","daemonset","node","cluster","minecraft","openshift","home assistant","home-assistant"])
                    weather_like_user = any(w in user_low for w in ["weather","forecast","temperature","rain","wind","snow","humidity"])
                    search_like_user = any(w in user_low for w in ["search","latest","news","according","web","internet","cite"]) and not k8s_like_user and not weather_like_user
                    claude_like_user = any(w in user_low for w in ["claude","claude code","code agent","code task","agent task"])
                    claude_status_user = claude_like_user and any(w in user_low for w in ["status","progress","how is","update","check","done","finished","result"])
                    claude_submit_user = claude_like_user and not claude_status_user
                    minio_like_user = claude_like_user and any(w in user_low for w in ["minio","bucket","upload","store","save to","put it in"])
                    payload = {"model": model, "messages": msg_with_hint, "tools": tools, "tool_choice": ("required" if (k8s_like_user or weather_like_user or search_like_user) else "auto"), "stream": False}
                    # Force temperature=0 and disable reasoning for fast voice responses
                    payload["temperature"] = 0.0
                    payload["options"] = {"think": False}  # Disable Ollama reasoning mode
                    if isinstance(top_p, (int, float)):
                        payload["top_p"] = float(top_p)
                    path = "/v1/chat/completions"
                else:
                    # Ollama direct path: force temperature=0 and disable reasoning
                    payload = {"model": model, "messages": messages, "stream": True, "options": {"num_predict": max_tokens, "think": False, "temperature": 0.0}}
                    if isinstance(top_p, (int, float)):
                        payload["options"]["top_p"] = float(top_p)
                    path = "/api/chat"
                payload["model"] = await ensure_model(client, model)
                log.info("/v1/responses [stream] using model=%s", payload["model"])
                # Verbose request logging
                log.info("=" * 60)
                log.info("USER> %s", user_text)
                log.info("SYSTEM> %s", (payload["messages"][0].get("content","") if payload["messages"] and payload["messages"][0].get("role")=="system" else "(none)")[:200])
                log.info("TOOLS> %s", ", ".join(t.get("function",{}).get("name","?") for t in payload.get("tools",[])))
                log.info("DETECT> k8s=%s weather=%s search=%s claude=%s minio=%s", k8s_like_user, weather_like_user, search_like_user, claude_like_user, minio_like_user)
                # Run tool-aware loop with non-stream calls, but stream deltas to HA
                if _is_llamastack():
                    payload["model"] = await ensure_model(client, model)
                    log.info("/v1/responses [llamastack tool-loop] using model=%s", payload["model"])
                    current_messages = payload["messages"][:]
                    # First turn
                    first = await post_json(client, path, payload)
                    choice = (first.get("choices") or [{}])[0]
                    msg = choice.get("message", {})
                    tool_calls = msg.get("tool_calls") or []
                    log.info("LLM-1> tool_calls=%d content=%r", len(tool_calls), (msg.get("content") or "")[:200])
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        log.info("LLM-1> tool_call: %s(%s)", fn.get("name"), json.dumps(json.loads(fn.get("arguments","{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments",{}))[:200])
                    # If the model returned no tools and gave us non-empty content:
                    # For k8s/weather queries, do NOT trust hallucinated answers — force auto-fallback
                    if not tool_calls and (msg.get("content")):
                        if not k8s_like_user and not weather_like_user and not claude_like_user:
                            log.info("RESPONSE> (direct, no tools) %s", (msg.get("content") or "")[:300])
                            log.info("=" * 60)
                            yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": msg.get("content")})
                            yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                            return
                        else:
                            log.info("/v1/responses [tool-loop] model returned text without tools for k8s/weather/claude query; forcing auto-fallback")

                    # If the model returned neither tools nor content, OR returned text-only for a k8s/weather query, auto-run tools
                    if not tool_calls:
                        # Heuristics based on the latest USER text (not assistant/system)
                        default_ns = _detect_default_namespace(current_messages)
                        if k8s_like_user:
                            cluster_query = any(w in user_low for w in ["cluster", "all namespace", "all pods", "overview"])
                            if default_ns:
                                yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": f"\n[tool] pods_list_in_namespace (auto) namespace={default_ns}..."})
                                auto_res = await k8s_pods_list_in_namespace(default_ns, None)
                                tool_name = "pods_list_in_namespace"
                            elif cluster_query:
                                yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": "\n[tool] cluster_operators (auto)..."})
                                auto_res = await k8s_cluster_operators()
                                tool_name = "cluster_operators"
                            else:
                                yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": "\n[tool] pods_list (auto) all namespaces..."})
                                auto_res = await k8s_pods_list(None, None)
                                tool_name = "pods_list"
                            log.info("AUTO-FALLBACK> k8s tool=%s", tool_name)
                            follow_messages = current_messages + [msg, {"role":"tool","name":tool_name,"content": json.dumps(auto_res)}]
                            try:
                                second = await post_json(client, path, {"model": payload["model"], "messages": follow_messages, "stream": False})
                                choice2 = (second.get("choices") or [{}])[0]
                                text2 = (choice2.get("message") or {}).get("content") or ""
                            except Exception as e:
                                log.warning("second-turn completion failed (k8s auto): %s", e)
                                text2 = ""
                            if not text2:
                                if "operators" in auto_res:
                                    text2 = _summarize_cluster_operators(auto_res)
                                elif "pods" in auto_res:
                                    text2 = _summarize_pods_list(auto_res) if default_ns else _summarize_pods_overview(auto_res)
                                else:
                                    text2 = str(auto_res)
                            log.info("RESPONSE> %s", text2[:300])
                            log.info("=" * 60)
                            yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text2})
                            yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                            return
                        if weather_like_user:
                            yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": "\n[tool] get_current_weather (auto) ..."})
                            auto_w = await weather_get_current(client, user_text, None, None, None)
                            follow_messages = current_messages + [msg, {"role":"tool","name":"get_current_weather","content": json.dumps(auto_w)}]
                            try:
                                second = await post_json(client, path, {"model": payload["model"], "messages": follow_messages, "stream": False})
                                choice2 = (second.get("choices") or [{}])[0]
                                text2 = (choice2.get("message") or {}).get("content") or ""
                            except Exception as e:
                                log.warning("second-turn completion failed (weather auto): %s", e)
                                text2 = ""
                            if not text2:
                                t = (auto_w or {}).get("temperature")
                                ws = (auto_w or {}).get("wind_speed")
                                if t:
                                    text2 = f"Current temperature is {t}, wind is {ws}."
                                else:
                                    text2 = "No weather data available."
                            log.info("RESPONSE> %s", text2[:300])
                            log.info("=" * 60)
                            yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text2})
                            yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                            return
                        if claude_like_user:
                            if claude_status_user:
                                yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": "\n[tool] claude_code_status (auto)..."})
                                auto_res = await claude_code_status(client)
                                text2 = _summarize_claude_code_tasks(auto_res)
                            else:
                                # Extract task prompt from user text by stripping claude keywords
                                task_prompt = user_text
                                for w in ["ask claude code to", "tell claude code to", "have claude code",
                                          "claude code", "code agent", "agent task",
                                          "and put it in minio", "and save to minio", "and upload to minio",
                                          "and store in minio", "put it in minio", "save to minio",
                                          "and put it in the bucket", "and upload it"]:
                                    task_prompt = task_prompt.lower().replace(w, "")
                                task_prompt = task_prompt.strip().strip(".,!?").strip()
                                if not task_prompt:
                                    task_prompt = user_text
                                if minio_like_user:
                                    yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": f"\n[tool] claude_code_submit_to_minio (auto)..."})
                                    auto_res = await claude_code_submit_to_minio(client, task_prompt)
                                    task_id = auto_res.get("task_id", "unknown")
                                    bucket = auto_res.get("minio_bucket", MINIO_DEFAULT_BUCKET)
                                    text2 = f"I've submitted that task to Claude Code. Task ID is {task_id}. When it's done, I'll automatically upload the results to the {bucket} bucket in MinIO."
                                else:
                                    yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": f"\n[tool] claude_code_submit (auto)..."})
                                    auto_res = await claude_code_submit(client, task_prompt)
                                    task_id = auto_res.get("task_id", "unknown")
                                    text2 = f"I've submitted that task to Claude Code. Task ID is {task_id}, status: {auto_res.get('status', 'unknown')}. You can ask me for the status anytime."
                            log.info("RESPONSE> %s", text2[:300])
                            log.info("=" * 60)
                            yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text2})
                            yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                            return
                        # Safe, short fallback for general queries
                        fallback = "It sounds like it’s been a tough evening. Try three quick steps: 1) swap out for 10 minutes so she can decompress, 2) slow breaths (in 4, hold 4, out 6 for 10 rounds), 3) a simple reset like a warm shower or short walk."
                        yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": fallback})
                        yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                        return
                        # Weather: try get_current_weather automatically using the USER question as the city name
                        if weather_like_user:
                            yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": "\n[tool] get_current_weather (auto) ..."})
                            auto_w = await weather_get_current(client, user_text, None, None, None)
                            follow_messages = current_messages + [msg, {"role":"tool","name":"get_current_weather","content": json.dumps(auto_w)}]
                            try:
                                second = await post_json(client, path, {"model": payload["model"], "messages": follow_messages, "stream": False})
                                choice2 = (second.get("choices") or [{}])[0]
                                text2 = (choice2.get("message") or {}).get("content") or ""
                            except Exception as e:
                                log.warning("second-turn completion failed (weather auto): %s", e)
                                text2 = ""
                            if not text2:
                                t = (auto_w or {}).get("temperature")
                                ws = (auto_w or {}).get("wind_speed")
                                if t:
                                    text2 = f"Current temperature is {t}, wind is {ws}."
                                else:
                                    text2 = msg.get("content") or "No weather data available."
                            yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text2})
                            yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                            return
                        # Web search: optional proxy-side fallback (disabled by default) — trigger on USER text
                        if WEB_FALLBACK_ENABLED and search_like_user:
                            yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": "\n[tool] web_search (proxy-fallback) ..."})
                            auto_s = await web_search_tavily(client, msg.get("content"))
                            follow_messages = current_messages + [msg, {"role":"tool","name":"web_search","content": json.dumps(auto_s)}]
                            try:
                                second = await post_json(client, path, {"model": payload["model"], "messages": follow_messages, "stream": False})
                                choice2 = (second.get("choices") or [{}])[0]
                                text2 = (choice2.get("message") or {}).get("content") or ""
                            except Exception as e:
                                log.warning("second-turn completion failed (web auto): %s", e)
                                text2 = ""
                            if not text2:
                                # synthesize minimalist summary from results
                                res = (auto_s or {}).get("results") or []
                                if res:
                                    lines = ["Top results:"]
                                    for i, r in enumerate(res[:3], 1):
                                        host = (r.get("url") or "").split("/")[2] if r.get("url") else ""
                                        lines.append(f"{i}) {r.get('title')} — {host}")
                                    text2 = "\n".join(lines)
                                else:
                                    text2 = (auto_s.get("error") if isinstance(auto_s, dict) and auto_s.get("error") else "No search results.")
                            yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text2})
                            yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                            return
                        # Otherwise emit the assistant's content
                        yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": msg.get("content")})
                        yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                        return
                    # Execute tools
                    results = []
                    default_ns = _detect_default_namespace(current_messages)
                    for tc in tool_calls:
                        name = tc.get("function",{}).get("name") or tc.get("name")
                        args = tc.get("function",{}).get("arguments") or tc.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {"raw": args}
                        # Normalize namespace to lowercase (k8s namespaces are case-sensitive, model often capitalizes)
                        if name in ("pods_list_in_namespace","pods_get","pods_log","pods_top") and args.get("namespace"):
                            args["namespace"] = args["namespace"].lower()
                        # When user asks about "cluster" without a specific namespace, redirect to cluster operators
                        cluster_query = any(w in user_low for w in ["cluster", "all namespace", "all pods", "overview"])
                        if cluster_query and not default_ns and name in ("pods_list_in_namespace", "pods_list", "web_search"):
                            log.info("/v1/responses [tool-exec] cluster query detected, redirecting %s -> cluster_operators", name)
                            name = "cluster_operators"
                            args = {}
                        # Inject namespace from user text when model omitted it or used generic "default"
                        if default_ns:
                            if name in ("pods_list_in_namespace","pods_get","pods_log","pods_top") and not args.get("all_namespaces"):
                                model_ns = args.get("namespace") or ""
                                if not model_ns or model_ns == "default":
                                    log.info("/v1/responses [tool-exec] injecting namespace %r -> %r for %s", model_ns, default_ns, name)
                                    args["namespace"] = default_ns
                        log.info("/v1/responses [tool-exec] name=%s args=%s", name, args)
                        yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": f"\n[tool] {name}..."})
                        result = {"error":"unknown tool"}
                        try:
                            if name == "get_current_weather":
                                # Execute via Open-Meteo fallback; model may pass "location" or "city"
                                city = args.get("city") or args.get("location") or args.get("name")
                                result = await weather_get_current(client, city, args.get("latitude"), args.get("longitude"), args.get("units"))
                            elif name == "get_weather_forecast":
                                city = args.get("city") or args.get("location") or args.get("name")
                                result = await weather_get_forecast(client, city, args.get("latitude"), args.get("longitude"), args.get("days"), args.get("units"))
                            elif name == "pods_list_in_namespace":
                                result = await k8s_pods_list_in_namespace(args.get("namespace","default"), args.get("labelSelector") or args.get("label_selector"))
                            elif name == "pods_list":
                                result = await k8s_pods_list(args.get("namespace"), args.get("labelSelector") or args.get("label_selector"))
                            elif name == "cluster_operators":
                                result = await k8s_cluster_operators()
                            elif name == "pods_get":
                                result = await k8s_pods_get(args.get("name"), args.get("namespace"))
                            elif name == "pods_log":
                                result = await k8s_pods_log(args.get("name"), args.get("namespace"), args.get("container"), args.get("tail"))
                            elif name == "pods_top":
                                result = await k8s_pods_top(args.get("namespace"), args.get("name"), args.get("all_namespaces"), args.get("label_selector") or args.get("labelSelector"))
                            elif name == "web_search":
                                # If the user asked about weather but model chose web_search, redirect to weather handler
                                if weather_like_user:
                                    log.info("/v1/responses [tool-exec] redirecting web_search to get_current_weather for weather query")
                                    # Extract city name from user text by stripping weather keywords
                                    city = user_text
                                    for w in ["what is the weather in", "what's the weather in", "weather in", "forecast for", "temperature in",
                                              "right now", "today", "tonight", "currently", "current", "weather"]:
                                        city = city.lower().replace(w, "")
                                    city = city.strip().strip("?.,!").strip()
                                    if not city:
                                        city = user_text  # fallback to full text
                                    log.info("/v1/responses [tool-exec] extracted city=%r from user_text=%r", city, user_text)
                                    result = await weather_get_current(client, city, None, None, None)
                                else:
                                    # Execute via LlamaStack tool runtime using server-configured provider (e.g., Tavily)
                                    result = await llamastack_invoke_tool_http(client, "web_search", args)
                            elif name == "claude_code_submit":
                                prompt = args.get("prompt") or args.get("task") or args.get("description") or ""
                                result = await claude_code_submit(client, prompt)
                            elif name == "claude_code_status":
                                result = await claude_code_status(client, args.get("task_id"))
                            elif name == "claude_code_submit_to_minio":
                                prompt = args.get("prompt") or args.get("task") or ""
                                bucket = args.get("bucket")
                                object_prefix = args.get("object_prefix", "claude-tasks")
                                result = await claude_code_submit_to_minio(client, prompt, bucket, object_prefix)
                            else:
                                # Try generic invocation on LlamaStack for any other server-side tool
                                result = await llamastack_invoke_tool_http(client, name, args)
                        except Exception as e:
                            log.exception("tool execution failed: %s", e)
                            result = {"error": f"tool execution failed: {e}"}
                        results.append({"role":"tool","tool_call_id": tc.get("id"), "name": name, "content": json.dumps(result)})
                        log.info("TOOL-RESULT> %s -> %s", name, json.dumps(result)[:300])
                    # If we have a cluster-wide pods list, summarize locally to avoid huge tool payloads
                    # For large tool results (cluster-wide), summarize locally instead of sending to LLM
                    if len(results) == 1 and results[0].get("name") in ("pods_list", "cluster_operators", "claude_code_submit", "claude_code_status", "claude_code_submit_to_minio"):
                        try:
                            data = json.loads(results[0].get("content") or "{}")
                            if "minio_bucket" in data:
                                task_id = data.get("task_id", "unknown")
                                bucket = data.get("minio_bucket", MINIO_DEFAULT_BUCKET)
                                text = f"I've submitted that task to Claude Code. Task ID is {task_id}. When it's done, I'll automatically upload the results to the {bucket} bucket in MinIO."
                                yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
                                yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                                return
                            if "task_id" in data or (isinstance(data, list) and data and "id" in data[0]):
                                text = _summarize_claude_code_tasks(data)
                                yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
                                yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                                return
                            if "operators" in data:
                                text = _summarize_cluster_operators(data)
                                yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
                                yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                                return
                            if "pods" in data:
                                text = _summarize_pods_overview(data)
                                yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
                                yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                                return
                        except Exception:
                            pass

                    # Continue with tool results
                    follow_messages = current_messages + [msg] + results
                    try:
                        second = await post_json(client, path, {"model": payload["model"], "messages": follow_messages, "stream": False})
                        choice2 = (second.get("choices") or [{}])[0]
                        text = (choice2.get("message") or {}).get("content") or ""
                        log.info("LLM-2> %s", text[:300])
                    except Exception as e:
                        log.warning("second-turn completion failed (tool results): %s", e)
                        text = ""
                    if not text:
                        # Try to synthesize a short summary from tool results
                        for r in results:
                            try:
                                data = json.loads(r.get("content") or "{}")
                                if "error" in data and len(data) <= 2:
                                    log.warning("/v1/responses [tool-result] tool %s returned error: %s", r.get("name"), data.get("error"))
                                    text = f"Sorry, the {r.get('name', 'tool')} returned an error. Please try again."
                                    break
                                if "task_id" in data or (isinstance(data, list) and data and "id" in data[0]):
                                    text = _summarize_claude_code_tasks(data)
                                    break
                                if "operators" in data:
                                    text = _summarize_cluster_operators(data)
                                    break
                                if "pods" in data:
                                    text = _summarize_pods_list(data)
                                    break
                                if "temperature" in data:
                                    text = f"Current temperature is {data.get('temperature')}, wind is {data.get('wind_speed')}."
                                    break
                                if "daily" in data:
                                    d = data.get("daily") or {}
                                    tmax = (d.get("temperature_2m_max") or [None])[0]
                                    tmin = (d.get("temperature_2m_min") or [None])[0]
                                    unit_label = "degrees Fahrenheit" if WEATHER_UNITS.lower() == "imperial" else "degrees Celsius"
                                    text = f"Today's forecast: high {tmax}, low {tmin} {unit_label}."
                                    break
                                # LlamaStack tool results (web_search, etc.)
                                if "result" in data:
                                    text = f"Here's what I found: {json.dumps(data.get('result'))[:500]}"
                                    break
                            except Exception:
                                pass
                    if not text:
                        log.warning("/v1/responses [fallback] no text from second LLM call and no summarizable tool results; raw results: %s",
                                    [r.get("content","")[:200] for r in results])
                        text = "I ran into an error processing tools. Please try again."
                    yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
                    yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"]}})
                    return
                async with client.stream("POST", f"{UPSTREAM}{path}", json=payload) as r:
                    r.raise_for_status()
                    async for raw in r.aiter_lines():
                        if not raw:
                            continue
                        line = raw.strip()
                        if line.startswith("data: "):
                            line = line[6:]
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if obj.get("done"):
                            usage = {"input_tokens": obj.get("prompt_eval_count"), "output_tokens": obj.get("eval_count")}
                            yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"], "usage": usage}})
                            return
                        msg = obj.get("message") or {}
                        if msg.get("thinking"):
                            yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": msg.get("thinking")})
                        if msg.get("content"):
                            yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": msg.get("content")})
                    else:
                        if obj.get("done"):
                            usage = {"input_tokens": obj.get("prompt_eval_count"), "output_tokens": obj.get("eval_count")}
                            yield ev("response.completed", {"response": {"id": response_id, "object": "response", "model": payload["model"], "usage": usage}})
                            return
                        msg = obj.get("message") or {}
                        if msg.get("thinking"):
                            yield ev("response.reasoning.delta", {"response_id": response_id, "item_id": item_id, "summary_index": 0, "delta": msg.get("thinking")})
                        if msg.get("content"):
                            yield ev("response.output_text.delta", {"response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": msg.get("content")})
        return StreamingResponse(sse_gen(), media_type="text/event-stream")

# Non-streaming path
    if _is_llamastack():
        payload = {"model": model, "messages": messages, "stream": False}
        # Force temperature=0 and disable reasoning for fast voice responses
        payload["temperature"] = 0.0
        payload["options"] = {"think": False}
        if isinstance(top_p, (int, float)):
            payload["top_p"] = float(top_p)
        path = "/v1/chat/completions"
    else:
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": {"num_predict": max_tokens, "think": False, "temperature": 0.0}}
        if isinstance(top_p, (int, float)):
            payload["options"]["top_p"] = float(top_p)
        path = "/api/chat"
    log.info("/v1/responses -> %s%s", UPSTREAM, path)
    async with httpx.AsyncClient() as client:
        payload["model"] = await ensure_model(client, model)
        log.info("/v1/responses using model=%s", payload["model"])
        comp = await post_json(client, path, payload)
    # Build minimal Responses-like output
    text = None
    try:
        # Prefer OpenAI chat shape
        text = comp.get("choices", [{}])[0].get("message", {}).get("content")
    except Exception:
        pass
    if not text:
        # Fallback for Ollama native shape
        try:
            text = comp.get("message", {}).get("content")
        except Exception:
            pass
    if not text:
        text = json.dumps(comp)
    out = {
        "id": comp.get("id","resp-1"),
        "object": "response",
        "created": comp.get("created"),
        "model": comp.get("model", model),
        "output": [ { "type": "output_text", "text": text } ],
    }
    return JSONResponse(out)

@app.post("/v1/chat/completions")
async def passthrough_chat(req: Request):
    body = await req.json()
    # Ensure non-streaming for JSON parse
    body.setdefault("stream", False)
    async with httpx.AsyncClient() as client:
        body["model"] = await ensure_model(client, body.get("model"))
        log.info("/v1/chat/completions using model=%s", body["model"])
        path = "/v1/chat/completions" if _is_llamastack() else "/api/chat"
        comp = await post_json(client, path, body)
    return JSONResponse(comp)

@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient() as client:
        if _is_llamastack():
            res = await get_json(client, "/v1/models")
            items = []
            for m in res.get("data", []):
                mid = m.get("identifier") or m.get("id")
                items.append({"id": mid, "object": "model", "owned_by": m.get("provider_id", "unknown")})
            out = {"object": "list", "data": items}
        else:
            tags = await get_json(client, "/api/tags")
            out = {"object":"list","data":[{"id":m.get("name"),"object":"model","owned_by":"ollama"} for m in tags.get("models",[]) ]}
    return JSONResponse(out)
@app.get("/health")
async def health():
    return {"ok": True, "upstream": UPSTREAM}
