# Troubleshooting

This document captures the issues we hit and the fixes that stabilized the end‑to‑end Assist pipeline (STT → LLM → TTS) on OpenShift.

## Symptoms and fixes

### 1) "Error talking to OpenAI" (404)
- Symptom: Assist fails; HA logs show 404 to OpenAI.
- Root cause: Our proxy initially mapped HA’s Responses API to `/v1/chat/completions` instead of Ollama’s native `/api/chat`.
- Fix: Map `/v1/responses` → `/api/chat` and `/v1/chat/completions` → `/api/chat` (compat shim). Add `/v1/models` → GET `/api/tags`.

### 2) "Error talking to OpenAI" (500)
- Symptom: 500 Internal Server Error from proxy; proxy logs show JSONDecodeError or ReadTimeout.
- Root cause: Ollama streamed JSON lines while the proxy tried to parse the full response as a single JSON.
- Fix: Force `stream=false` for non‑SSE path and parse the non‑stream JSON; later we implemented full SSE streaming (see below).

### 3) "Unable to get response"
- Symptom: Assist pipeline starts but HA reports it cannot get a response.
- Root cause: HA’s OpenAI Conversation integration expects OpenAI Responses‑style Server‑Sent Events (SSE). Our proxy returned a single JSON object.
- Fix: Implement proper SSE:
  - Emit `response.created`
  - Emit `response.output_text.delta` chunks
  - Emit `response.completed` with basic usage

### 4) Infinite loop: "Hello how can I assist you today" / "your message was empty"
- Symptom: Assistant repeats boilerplate and/or claims the user message is empty.
- Root cause: HA sends the prompt as Responses `input` items, often as `{"type":"message","role":...,"content":[...]}`. The proxy only concatenated `input_text` items and dropped message items.
- Fix: Robust input mapping:
  - Parse `type=message` items; map role `developer` → `system`
  - Extract text from `content` arrays of `{type: input_text | text}`
  - Preserve user/assistant messages; fall back to string input

### 5) Unknown model IDs (HA sends `gpt-4o`)
- Symptom: Ollama returns 404 for unknown model names; Assist fails.
- Root cause: HA defaults to OpenAI model names unless overridden.
- Fix: In proxy, check `/api/tags` and fall back to `gpt-oss:120b` for unknown models. `GET /v1/models` now reflects Ollama tags.

### 6) Proxy recursion / wrong upstream
- Symptom: Proxy calls itself; requests hang or error.
- Root cause: `OLLAMA_URL` (OpenAI‑style) and `UPSTREAM_BASE` (native) pointed to the same service.
- Fix: Use two secrets:
  - `OLLAMA_URL` → HA uses this as its OpenAI base (the proxy URL)
  - `OLLAMA_UPSTREAM` → proxy uses this to reach Ollama native

### 7) Device cannot reach Home Assistant (fetching audio)
- Symptom: Voice satellite says it cannot reach HA.
- Root cause: HA `internal_url` defaulted to a cluster‑internal DNS that the device could not reach.
- Fix: Postinstall job writes `internal_url` and `external_url` from secret into `configuration.yaml` and restarts HA. Set `HA_INTERNAL_URL` to the HA NodePort on your LAN node.

### 8) OpenAI TTS: "Invalid options: ['voice']"
- Symptom: Kokoro voice option rejected.
- Root cause: The custom OpenAI TTS integration didn’t include `voice` in supported options.
- Fix: Patched `supported_options` in the component to include `voice`. Note: Ensure future containers include this change or re‑apply the patch.

### 9) STT language mismatch
- Symptom: Pipeline errors complaining about missing `stt_language`.
- Fix: Configure job enforces `en` for STT and pipeline language.

### 10) ESPHome not discovered automatically
- Symptom: Device not found in HA UI.
- Root cause: mDNS broadcast doesn’t cross into the cluster.
- Fix: Add device by IP (port 6053). Reserve DHCP lease and set an API key.

## Useful commands

- Inspect recent HA logs for pipeline/response errors:
```
oc logs -n home-assistant deploy/home-assistant --since=2m | egrep -i "openai|assist|pipeline|error"
```
- Inspect proxy logs (shows upstream mapping and model fallback):
```
oc logs -n home-assistant deploy/openai-proxy --since=2m --tail=200
```
- Test proxy SSE directly from the HA pod:
```
oc exec -n home-assistant deploy/home-assistant -- sh -lc 'cat <<EOF | curl -sN -H "accept: text/event-stream" -H "content-type: application/json" -X POST -d @- http://openai-proxy.home-assistant.svc.cluster.local:8005/v1/responses
{"model":"gpt-oss:120b","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"Say exactly: pong"}]}],"stream":true}
EOF'
```
- Verify Ollama models via proxy:
```
oc exec -n home-assistant deploy/home-assistant -- sh -lc 'curl -sS http://openai-proxy.home-assistant.svc.cluster.local:8005/v1/models | jq .'
```

## Configuration checklist
- `ha-api-credentials` contains: `HA_BASE`, `HA_TOKEN`, `HA_INTERNAL_URL`, `HA_EXTERNAL_URL`, `KOKORO_*`, `TTS_ENTITY_ID`, `OLLAMA_URL`, `OLLAMA_UPSTREAM`, `OLLAMA_MODEL`.
- NodePorts are reachable from LAN (HA, Whisper, Kokoro) and Routes are created for external access as needed.
- Default Assist pipeline selects: STT=Wyoming Whisper, Conversation=OpenAI Conversation, TTS=OpenAI TTS (Kokoro), language=en.
