#!/usr/bin/env bash
# Sets up or updates the whole Gyan stack on a server. Run it ON the server, from the repo folder:
#
#     GYAN_HOST=gyan.example.com bash scripts/vm-bootstrap.sh
#
# Safe to run again: the first run creates .env, pulls the model and makes the UI's gateway key;
# later runs just rebuild and restart whatever changed. Existing data is never touched.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${GYAN_HOST:-}"

command -v docker >/dev/null || { echo "Docker is not installed."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "The Docker compose plugin is missing (apt install docker-compose-v2)."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Cannot talk to Docker. Add yourself to the docker group: sudo usermod -aG docker \$USER, then log in again."; exit 1; }

echo "== 1/5 settings (.env)"
if [ ! -f .env ]; then
  if [ -n "$HOST" ]; then python3 scripts/init_env.py --host "$HOST" --http; else python3 scripts/init_env.py; fi
else
  echo ".env already exists, keeping it"
fi

echo "== 2/5 gateway, Redis, Ollama, SearXNG"
docker compose up -d --build
echo -n "waiting for the gateway "
for _ in $(seq 1 60); do
  if docker compose exec -T gateway python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" >/dev/null 2>&1; then echo " ready"; break; fi
  echo -n "."; sleep 2
done

echo "== 3/5 model"
MODEL="$(grep -E '^DEFAULT_MODEL=' .env | cut -d= -f2- || true)"; MODEL="${MODEL:-qwen2.5:3b}"
INSTALLED="$(docker compose exec -T ollama ollama list || true)"
case "$INSTALLED" in
  *"$MODEL"*) echo "$MODEL is already installed" ;;
  *) docker compose exec -T ollama ollama pull "$MODEL" ;;
esac
# Image readers: the OCR model for documents and the vision model for photos.
OCR="$(grep -E '^OCR_MODEL=' .env | cut -d= -f2- || true)"; VISION="$(grep -E '^VISION_MODEL=' .env | cut -d= -f2- || true)"
for helper in "${OCR:-glm-ocr}" "${VISION:-qwen2.5vl:3b}"; do
  case "$INSTALLED" in
    *"$helper"*) echo "$helper is already installed" ;;
    *) docker compose exec -T ollama ollama pull "$helper" ;;
  esac
done

echo "== 4/5 the chat UI's gateway key"
if grep -qE '^GYAN_API_KEY=.+' .env; then
  echo "GYAN_API_KEY already set"
else
  KEY="$(docker compose exec -T gateway gyan-gateway create-key --name gyan-ui --rpm 300 --daily 5000 | tail -1 | tr -d '\r')"
  case "$KEY" in sk-gyan-*) ;; *) echo "Could not create the UI key."; exit 1 ;; esac
  python3 - "$KEY" <<'PY'
import sys, pathlib
key = sys.argv[1]
p = pathlib.Path(".env")
lines = p.read_text(encoding="utf-8").splitlines()
p.write_text("\n".join(f"GYAN_API_KEY={key}" if l.startswith("GYAN_API_KEY=") else l for l in lines) + "\n", encoding="utf-8")
PY
  echo "saved the key in .env (prefix ${KEY:0:12}...)"
fi

echo "== 5/5 chat UI"
docker compose --profile ui up -d
echo
docker compose --profile ui ps --format 'table {{.Name}}\t{{.Status}}'
