#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
  echo 'Usage: bash run-low-vram.sh [--cpu] [--agent|--abliterated|--model MODEL] [--data PATH]'
  echo 'Models: qwen3.5-2b, qwen3.5-0.8b, qwen2.5-1.5b, qwen3.5-2b-abliterated, minicpm5-2b'
  echo '--agent selects MiniCPM5 2B; --abliterated selects Huihui Qwen3.5 2B.'
  exit 0
fi
PY="$ROOT/.venv-low-vram/bin/python"
if [ ! -x "$PY" ] || [ ! -x "$ROOT/derived/llm/bin-linux/lastbook-llm" ]; then
  echo 'Run bash install-low-vram.sh first.' >&2
  exit 1
fi
export LASTBOOK_AI_PROFILE=low-vram
export LASTBOOK_AI_MODEL="${LASTBOOK_AI_MODEL:-qwen3.5-2b}"
export LASTBOOK_LOCAL_AI=1
export LASTBOOK_AI_IDLE_S="${LASTBOOK_AI_IDLE_S:-300}"
backend=OFF
if [ -f "$ROOT/derived/llm/low-vram-backend" ]; then read -r backend < "$ROOT/derived/llm/low-vram-backend"; fi
if [ "$backend" = OFF ]; then export LASTBOOK_AI_CPU_ONLY=1; fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    --cpu) export LASTBOOK_AI_CPU_ONLY=1; shift ;;
    --agent) export LASTBOOK_AI_MODEL=minicpm5-2b; shift ;;
    --abliterated) export LASTBOOK_AI_MODEL=qwen3.5-2b-abliterated; shift ;;
    --model)
      [ "$#" -ge 2 ] || { echo '--model requires a model name.' >&2; exit 2; }
      export LASTBOOK_AI_MODEL="$2"; shift 2 ;;
    *) break ;;
  esac
done
case "$LASTBOOK_AI_MODEL" in qwen3.5-2b|qwen3.5-0.8b|qwen2.5-1.5b|qwen3.5-2b-abliterated|minicpm5-2b) ;; *) echo 'Unknown model. Run bash run-low-vram.sh --help.' >&2; exit 2 ;; esac
export GST_PLUGIN_PATH_1_0="$ROOT/derived/gst${GST_PLUGIN_PATH_1_0:+:$GST_PLUGIN_PATH_1_0}"
export WEBKIT_GST_ALLOWED_URI_PROTOCOLS=lastbook
# Older 2 GB cards may have limited compositor/DMABUF support. This also leaves more VRAM for the model.
export WEBKIT_DISABLE_DMABUF_RENDERER="${WEBKIT_DISABLE_DMABUF_RENDERER:-1}"
exec "$PY" -B "$ROOT/app/main.py" "$@"
