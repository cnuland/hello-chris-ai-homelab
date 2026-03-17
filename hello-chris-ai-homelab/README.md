# Home Assistant on OpenShift: Voice Assistant Stack

See also:
- Architecture: Architecture.md
- Troubleshooting: troubleshooting.md

This repo contains infra-as-code to deploy Home Assistant and a full voice stack on OpenShift:
- STT: Wyoming Whisper (rhasspy/wyoming-whisper)
- TTS: Kokoro FastAPI (OpenAI-compatible) + custom Home Assistant integration (OpenAI TTS)
- HA add-ons bootstrap (HACS + OpenAI TTS), postinstall cleanup
- Automated configuration job that wires STT/TTS and sets a default pipeline

The manifests are idempotent. Re-running the steps is safe.

## Prerequisites
- OpenShift cluster and `oc` CLI logged in with permissions to create namespaces, deployments, services, routes, jobs.
- Internet egress to pull container images and GitHub assets.
- A Home Assistant Long-Lived Access Token (LLT).

## Components and namespaces
- Namespace `home-assistant`: Home Assistant core, OpenAI proxy, add-on jobs, configuration jobs.
- Namespace `voice`: Wyoming Whisper (STT) and Kokoro TTS services.
- Namespace `gpt-oss`: Ollama models service.

Kustomize manifests will create namespaces automatically.

## End-to-end architecture (high level)
- Input: An ESPHome device records audio and sends it to Home Assistant.
- STT: HA forwards audio to Wyoming Whisper (TCP 10300) to transcribe to text.
- Conversation: HA’s OpenAI Conversation integration calls our in-cluster OpenAI proxy at `/v1/responses`.
- LLM: The proxy translates Requests to Ollama’s native `/api/chat` and returns OpenAI Responses-compatible streaming events.
- TTS: HA calls the OpenAI TTS custom integration which sends text to Kokoro (OpenAI-compatible) to synthesize audio.
- Playback: HA serves the resulting audio file URL; the device fetches it via HA’s `internal_url`.

See Architecture.md for full details.

## Quick start
1) Export your Home Assistant token in your shell (do NOT paste it into the file):

```
export HA_TOKEN=<your_long_lived_token>
```

2) Create/patch the HA API secret with all required fields (safe to re-run):

```
oc -n home-assistant create secret generic ha-api-credentials \
  --from-literal=HA_BASE=http://home-assistant.home-assistant.svc.cluster.local:8123 \
  --from-literal=HA_TOKEN="$HA_TOKEN" \
  --from-literal=STT_HOST=wyoming-whisper.voice.svc.cluster.local \
  --from-literal=STT_PORT=10300 \
  --from-literal=KOKORO_URL=http://kokoro-tts.voice.svc.cluster.local:8880/v1 \
  --from-literal=KOKORO_MODEL=kokoro \
  --from-literal=KOKORO_VOICE=bf_emma \
  --from-literal=KOKORO_SPEED=1.0 \
  --from-literal=KOKORO_INSTRUCTIONS="Use a warm, flirtatious British accent." \
  --from-literal=TTS_ENTITY_ID=tts.openai_tts_kokoro \
  --dry-run=client -o yaml | oc apply -f -
```

3) Deploy services in the `voice` namespace:

```
# Wyoming Whisper (STT)
oc -n voice apply -k .k8s/stt-whisper

# Kokoro TTS (OpenAI-compatible) + NodePort + Route
oc -n voice apply -k .k8s/tts-kokoro
```

4) Deploy Home Assistant (namespace, SA, PVC, Service, Route, NodePort):

```
oc -n home-assistant apply -k .k8s
```

5) Install add-ons into HA config volume (HACS + OpenAI TTS integration):

```
oc -n home-assistant apply -f .k8s/ha-addons/cm-installer.yaml -f .k8s/ha-addons/job.yaml
# Watch logs until job completes
oc -n home-assistant logs -l job-name=ha-addon-installer --follow
```

6) Postinstall cleanup (ensures custom integrations load cleanly, restarts HA):

```
oc -n home-assistant apply -f .k8s/ha-addons/cm-postinstall.yaml -f .k8s/ha-addons/job-postinstall.yaml
oc -n home-assistant logs -l job-name=ha-postinstall --follow
```

7) Configure HA via automation (create OpenAI TTS entry, wire STT/TTS, set default pipeline):

```
oc -n home-assistant apply -f .k8s/ha-api/cm-scripts.yaml -f .k8s/ha-api/job-configure-voice.yaml
oc -n home-assistant logs -l job-name=ha-configure-voice --follow
```

The configure job:
- Ensures a Wyoming Whisper config entry pointing to `wyoming-whisper.voice.svc.cluster.local:10300`.
- Tries to create an OpenAI TTS entry that points at Kokoro using values from `ha-api-credentials` secret (model/voice/speed/instructions).
- Selects an existing pipeline and sets it as default (uses multiple WS command variants for compatibility).

## Verifying the deployment
- Whisper in-cluster:

```
oc exec -n home-assistant deploy/home-assistant -- sh -lc 'nc -zvw2 wyoming-whisper.voice.svc.cluster.local 10300 && echo OK'
```

- Kokoro in-cluster:

```
oc exec -n home-assistant deploy/home-assistant -- sh -lc 'curl -fsS http://kokoro-tts.voice.svc.cluster.local:8880/health && echo'
```

- Kokoro via Route (TLS edge):

```
curl -sk https://kokoro-tts-voice.apps.<your-domain>/health
```

- NodePort access (LAN): find node IP(s) and ports:

```
oc get nodes -o wide
oc get svc -n home-assistant home-assistant-nodeport -o wide    # 8123:NNNNN
oc get svc -n voice wyoming-whisper-nodeport -o wide            # 10300:NNNNN
oc get svc -n voice kokoro-tts-nodeport -o wide                 # 8880:31880 (pinned)
```

- HA TTS entity (after configure job): `tts.openai_tts_kokoro`.
- LLM (OpenAI Conversation) is auto-installed; HA is configured to use your Ollama route via env OPENAI_BASE_URL / OPENAI_API_BASE.

## LLM (Ollama) hookup via OpenAI proxy
We use an in-cluster proxy that makes HA’s new OpenAI Responses API work with Ollama’s `/api/chat`.

- Proxy service: `openai-proxy.home-assistant.svc.cluster.local:8005/v1`
- The HA deployment reads:
  - `OPENAI_BASE_URL` / `OPENAI_API_BASE` → points to the proxy base (`http://openai-proxy.home-assistant.svc.cluster.local:8005/v1`)
  - `OPENAI_API_KEY` → `unused`
- Proxy upstream:
  - `OLLAMA_UPSTREAM` (native) points to `http://ollama-gpt-oss-120b.gpt-oss.svc.cluster.local:11434`
  - Proxy maps `/v1/responses` → `/api/chat` with SSE streaming and message-role translation
  - Unknown models auto-fallback to `gpt-oss:120b`; `/v1/models` maps to GET `/api/tags`

Secret keys in `ha-api-credentials` (safe to re-apply):
- `OLLAMA_URL`: proxy base URL used by HA (OpenAI-compatible)
- `OLLAMA_UPSTREAM`: Ollama native base used by the proxy
- `OLLAMA_MODEL`: preferred model id (e.g. `gpt-oss:120b`)

To change the model or bases:
```
oc -n home-assistant patch secret ha-api-credentials --type=strategic \
  -p '{"stringData":{"OLLAMA_URL":"http://openai-proxy.home-assistant.svc.cluster.local:8005/v1","OLLAMA_UPSTREAM":"http://ollama-gpt-oss-120b.gpt-oss.svc.cluster.local:11434","OLLAMA_MODEL":"gpt-oss:120b"}}'
oc -n home-assistant rollout restart deploy/home-assistant
```

## Tuning (runtime)
- Token cap for replies (prevents long generations that can stall TTS): set PROXY_MAX_TOKENS_CAP in the secret and restart the proxy.
```
oc -n home-assistant patch secret ha-api-credentials --type=merge -p '{"stringData":{"PROXY_MAX_TOKENS_CAP":"120"}}'
oc -n home-assistant rollout restart deploy/openai-proxy
```
- Default max tokens if HA doesn’t send one:
```
oc -n home-assistant patch secret ha-api-credentials --type=merge -p '{"stringData":{"PROXY_DEFAULT_MAX_TOKENS":"256"}}'
oc -n home-assistant rollout restart deploy/openai-proxy
```
- TTS HTTP timeout (seconds) for the OpenAI TTS integration patch applied by the postinstall job:
```
oc -n home-assistant patch secret ha-api-credentials --type=merge -p '{"stringData":{"OPENAI_TTS_TIMEOUT":"90"}}'
oc -n home-assistant delete job ha-postinstall --ignore-not-found
oc -n home-assistant apply -f .k8s/ha-addons/job-postinstall.yaml
oc -n home-assistant logs -l job-name=ha-postinstall --follow
```
- Weather tool fallback (Open‑Meteo via proxy) defaults:
```
oc -n home-assistant patch secret ha-api-credentials --type=merge -p '{"stringData":{"WEATHER_UNITS":"metric"}}'
oc -n home-assistant rollout restart deploy/openai-proxy
```

## Verification script (HA + Ollama)
A helper script lives at `scripts/verify-llm.sh` to validate both the Ollama endpoint and an HA conversation round‑trip.

Usage:
```
export HA_TOKEN=<your_long_lived_token>
# Optional overrides
# export HASS_BASE=https://home-assistant-home-assistant.apps.<your-domain>
# export OLLAMA_BASE=https://ollama-gpt-oss-120b-gpt-oss.apps.<your-domain>/v1

bash scripts/verify-llm.sh
```
It prints:
- Ollama models seen at `$OLLAMA_BASE/v1/models`
- A one‑line reply from Home Assistant `/api/conversation/process`

## Changing voice, speed, or instructions
You can change the defaults in the secret, then re-run the configure job:

```
oc -n home-assistant patch secret ha-api-credentials --type=strategic \
  -p '{"stringData":{"KOKORO_VOICE":"bf_emma","KOKORO_SPEED":"1.0","KOKORO_INSTRUCTIONS":"Use a warm, flirtatious British accent."}}'

oc -n home-assistant delete job ha-configure-voice --ignore-not-found
oc -n home-assistant apply -f .k8s/ha-api/job-configure-voice.yaml
oc -n home-assistant logs -l job-name=ha-configure-voice --follow
```

Alternatively, you can adjust options via HA’s config entry (UI or REST options flow). The automation already sets voice/instructions when creating the entry.

## Files touched in this repo (high level)
- `.k8s/kustomization.yaml` — Home Assistant base (includes NodePort and Route).
- `.k8s/stt-whisper/kustomization.yaml` — now includes `service-nodeport.yaml`.
- `.k8s/tts-kokoro/kustomization.yaml` — includes `service-nodeport.yaml` and Route.
- `.k8s/ha-addons/cm-installer.yaml` + `job.yaml` — installs HACS + OpenAI TTS into `/config/custom_components`.
- `.k8s/ha-addons/cm-postinstall.yaml` + `job-postinstall.yaml` — cleans stray files in custom_components and restarts HA.
- `.k8s/ha-api/secret.yaml` — carries HA_BASE, KOKORO_* defaults, TTS_ENTITY_ID. HA_TOKEN stays a placeholder.
- `.k8s/ha-api/cm-scripts.yaml` — `configure.py` automates OpenAI TTS creation, Whisper wiring, and default pipeline selection.
- `.k8s/ha-api/job-configure-voice.yaml` — runs the automation inside a Job.

## URLs for device access
Home Assistant must expose a LAN-reachable `internal_url` so devices can fetch TTS audio.
- The postinstall job writes `internal_url` and `external_url` from the secret into `configuration.yaml` and restarts HA.
- Set `HA_INTERNAL_URL` to your HA NodePort (e.g. `http://<node-ip>:31273`).
- `HA_EXTERNAL_URL` should be the HA Route.

## ESPHome adoption on a cluster
Auto-discovery via mDNS won’t work across your cluster boundary. Add by IP: `http://<device-ip>:6053` in ESPHome.

## Troubleshooting
See troubleshooting.md for the full catalog. Quick tips:
- HTTP 401/WS auth failed in configure job: ensure `ha-api-credentials` contains a valid `HA_TOKEN` (LLT).
- “Integration 'openai_tts' not found”: run the add-on installer job and the postinstall cleanup, or ensure `/config/custom_components/openai_tts` exists; also ensure no stray files are at `/config/custom_components` root (postinstall job handles this).
- Pipeline create returns `invalid_format`: the job will fall back to selecting an existing pipeline and set it as default using a compatible WS command.
- Whisper/Kokoro connectivity: exec from the HA pod and curl the Service/Route as shown above.

## Recreate from zero (order of ops)
1) Create/patch `ha-api-credentials` with your HA token and Kokoro defaults.
2) Deploy `voice` workloads (Whisper + Kokoro).
3) Deploy Home Assistant.
4) Run add-on installer job, then postinstall job.
5) Run configure job, confirm logs show “Created OpenAI TTS entry for Kokoro” and default pipeline set.

## Clean-up

```
oc delete ns voice
oc delete ns home-assistant
```

(Images and PVCs may persist depending on your cluster’s storage class/policy.)

## Notes
- Keep your LLT out of files. Always supply it via environment variable when creating the secret.
- Manifests are designed to be re-applied safely.
