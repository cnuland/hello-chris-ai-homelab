# Architecture

This system runs a complete Home Assistant (HA) voice stack on OpenShift. It is split across three namespaces and uses a small OpenAI‑compatible proxy to connect HA’s new Responses API to Ollama.

## Namespaces and core services

- home-assistant
  - Home Assistant core (Deployment + PVC + Service + Route + NodePort)
  - OpenAI proxy (FastAPI/uvicorn) exposing `/v1` for HA
  - Jobs:
    - Add‑on installer (installs HACS + OpenAI TTS into `/config/custom_components`)
    - Postinstall (cleans component folder, writes internal/external URLs, restarts HA)
    - Configure voice (creates STT/TTS/Conversation entries; sets default pipeline)
- voice
  - Wyoming Whisper STT (rhasspy/wyoming‑whisper)
  - Kokoro TTS (ghcr.io/remsky/kokoro-fastapi-cpu), OpenAI‑compatible `/v1`
- gpt-oss
  - Ollama server with local models (e.g., `gpt-oss:120b`, `gpt-oss:20b`)

## Networking

- ClusterIP Services for in‑cluster communication.
- NodePorts to expose to your LAN:
  - Home Assistant NodePort (maps 8123 → NNNNN)
  - Wyoming Whisper NodePort (10300 → 31615)
  - Kokoro TTS NodePort (8880 → 31880)
- Routes for external HTTPS access:
  - HA web (optional)
  - Kokoro (optional)
  - Ollama (optional; primarily used for manual checks)
- HA internal/external URLs:
  - Internal: must be LAN‑reachable by devices (we set it to HA’s NodePort)
  - External: HA Route

## Secrets and configuration

Secret `ha-api-credentials` holds:
- HA_BASE, HA_TOKEN (LLT)
- HA_INTERNAL_URL, HA_EXTERNAL_URL
- STT_HOST/PORT (Wyoming Whisper)
- KOKORO_URL/MODEL/VOICE/SPEED/INSTRUCTIONS and TTS_ENTITY_ID
- OLLAMA_URL (OpenAI base for HA → the proxy), OLLAMA_UPSTREAM (Ollama native for the proxy), OLLAMA_MODEL

Jobs read these values to install add‑ons, patch HA config, and program pipelines.

## Data flow (Assist round‑trip)

1) Capture: ESPHome device sends audio to HA.
2) STT: HA streams audio to Wyoming Whisper over TCP 10300; returns text.
3) Conversation: HA’s OpenAI Conversation integration sends a Responses request to the proxy at `/v1/responses`.
4) Proxy:
   - Translates Responses input (message items, developer → system) into Ollama chat messages
   - Falls back unknown models to `gpt-oss:120b`
   - Calls Ollama `/api/chat`
   - Streams back Responses‑style SSE events (response.created, response.output_text.delta, response.completed)
5) TTS: HA calls the OpenAI TTS integration; Kokoro synthesizes audio and returns an audio URL.
6) Playback: Device downloads audio from HA via `internal_url`.

## OpenAI proxy details

- Endpoints exposed:
  - GET `/v1/models` → Ollama GET `/api/tags`
  - POST `/v1/responses` → Ollama POST `/api/chat`
  - POST `/v1/chat/completions` → Ollama POST `/api/chat` (compat)
- Streaming: Emits OpenAI Responses SSE events; aggregates Ollama JSON lines.
- Input mapping: Accepts Responses `input` array and `messages`; map roles user/assistant/developer; extract text from `input_text` parts.
- Model handling: Validates against tags once; if unknown (e.g. `gpt-4o`), falls back to `gpt-oss:120b`.

## Home Assistant automation

- Add‑on installer job: downloads and places custom integrations (HACS + OpenAI TTS).
- Postinstall job: removes stray files, writes `internal_url` / `external_url`, restarts HA.
- Configure job: creates Whisper + OpenAI TTS + OpenAI Conversation entries and sets default Assist pipeline with language `en` and voice `bf_emma`.

## Observability

- Use `oc logs` on the HA and proxy deployments. The proxy logs include upstream mapping and model fallbacks.
- `scripts/verify-llm.sh` checks Ollama models and a basic HA conversation call.

## Security

- Do not commit your HA long‑lived token. Inject it via environment and create the secret at deploy time.
- NodePorts expose services on your LAN; restrict access appropriately.

## Planned extensions

We plan to integrate additional capabilities alongside the current path:

- LlamaStack (tooling and orchestration layer)
  - Provide a standardized interface for models, tools, and routing policies
  - Could front our existing proxy or be fronted by it, depending on direction
- Kubernetes MCP (Model Context Protocol) server
  - Host MCP tool providers in‑cluster (web search, code interpreter, sensors/actuators)
  - Allow HA’s LLM agent to call tools via MCP; either through OpenAI tool calls or a sidecar agent
- Additional tool providers
  - Web search, code execution, file/image tools, HA‑native service tools
  - Expose via MCP and/or directly via HA OpenAI tools APIs

These components will be documented and wired into the pipeline in a future phase while preserving the current stable path (HA → proxy → Ollama).
