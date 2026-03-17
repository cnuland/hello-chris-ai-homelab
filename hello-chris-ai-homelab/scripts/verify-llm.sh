#!/usr/bin/env bash
set -euo pipefail

# Config (can override via env)
HASS_BASE=${HASS_BASE:-https://home-assistant-home-assistant.apps.ironman.cjlabs.dev}
OLLAMA_BASE=${OLLAMA_BASE:-https://ollama-gpt-oss-120b-gpt-oss.apps.ironman.cjlabs.dev/v1}
: "${HA_TOKEN:?Set HA_TOKEN to your Home Assistant Long-Lived Token}"

say() { printf '%s\n' "$*"; }
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

say "[1/2] Checking Ollama models at $OLLAMA_BASE/v1/models"
MODELS_JSON=$(curl -sk "$OLLAMA_BASE/models" || curl -sk "$OLLAMA_BASE/v1/models") || err "Unable to reach Ollama"
# Print model IDs (supports both OpenAI /v1 and legacy)
python3 - "$MODELS_JSON" <<'PY'
import sys, json
raw=sys.argv[1]
try:
  obj=json.loads(raw)
except Exception:
  print("(non-JSON response)")
  sys.exit(0)
if isinstance(obj, dict) and obj.get('object')=='list' and isinstance(obj.get('data'), list):
  print('models:', ', '.join([m.get('id','') for m in obj['data']]))
elif isinstance(obj, dict) and isinstance(obj.get('models'), list):
  print('models:', ', '.join([m.get('name','') or m.get('model','') for m in obj['models']]))
else:
  print(obj)
PY

say "[2/2] Calling HA conversation API"
PROMPT=${PROMPT:-"Say 'hello from Ollama'. Keep it short."}
RESP=$(curl -sk -H "Authorization: Bearer $HA_TOKEN" -H 'Content-Type: application/json' \
  -X POST "$HASS_BASE/api/conversation/process" \
  --data "{\"text\":\"$PROMPT\",\"language\":\"en\"}") || err "Conversation API call failed"

python3 - "$RESP" <<'PY'
import sys, json
raw=sys.argv[1]
try:
  obj=json.loads(raw)
except Exception:
  print(raw)
  sys.exit(0)
# HA response shapes vary; try common paths
msg = (obj.get('response') or {}).get('speech') or {}
plain = msg.get('plain') or {}
text = plain.get('speech') or obj.get('response') or obj
print('HA reply:', (text if isinstance(text, str) else json.dumps(text)))
PY

say "Done. If HA reply arrived, your LLM pipeline is reachable."