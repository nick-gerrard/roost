#!/usr/bin/env bash
# Run from your laptop: sync roost to the server and (re)start roostd. No sudo.
#   bootstrap/push.sh            # sync code, uv sync, restart roostd if running
#   bootstrap/push.sh --seed     # also seed registry + adopt env for milliways/pwhl (first time)
set -euo pipefail
HOST="${ROOST_SSH_HOST:-linode}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
    --exclude .git "$HERE/" "$HOST:roost/"
ssh "$HOST" 'cd ~/roost && ~/.local/bin/uv sync -q && mkdir -p ~/.roost/{env,staged,logs} && chmod 700 ~/.roost ~/.roost/env'

if [[ "${1:-}" == "--seed" ]]; then
    ssh "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
R=~/.roost
# 1. Shared secret for webhook HMAC + API bearer token.
if [[ ! -f $R/roostd.env ]]; then
    umask 077
    printf 'ROOST_SECRET=%s\nROOST_RESERVED_PORTS=3001,5001,5432,8001,8900,9009\n' "$(openssl rand -hex 32)" > $R/roostd.env
    echo "generated $R/roostd.env"
fi
# 2. Registry seeded with the ports these apps already run on.
if [[ ! -f $R/registry.json ]]; then
    cat > $R/registry.json <<'JSON'
{
  "milliways": {"name": "milliways", "repo_name": "milliways",
    "repo_url": "https://github.com/nick-gerrard/milliways.git",
    "subdomain": "milliways", "on": "push", "backend_port": 8002, "frontend_port": 3002},
  "pwhl": {"name": "pwhl", "repo_name": "pwhl-stats",
    "repo_url": "https://github.com/nick-gerrard/pwhl-stats.git",
    "subdomain": "pwhl", "on": "ci-pass", "backend_port": 8000, "frontend_port": null}
}
JSON
    echo "seeded $R/registry.json"
fi
# 3. Move runtime env out of the hand-written units into roost env files
#    (values stay on the server; only key names are printed).
umask 077
if [[ ! -f $R/env/milliways.env ]]; then
    sed -nE 's/^Environment="?([A-Za-z_][A-Za-z0-9_]*=.*[^"])"?$/\1/p' \
        /etc/systemd/system/milliways-api.service > $R/env/milliways.env
    echo "milliways.env keys: $(cut -d= -f1 $R/env/milliways.env | tr '\n' ' ')"
fi
if [[ ! -f $R/env/pwhl.env ]]; then
    cp ~/pwhl-stats/.env $R/env/pwhl.env
    echo "pwhl.env keys: $(cut -d= -f1 $R/env/pwhl.env | tr '\n' ' ')"
fi
EOF
fi

# Restart roostd if it's installed (Restart=always brings it back; no sudo needed).
ssh "$HOST" 'pkill -u "$USER" -f "uvicorn roostd.server:app" && echo "roostd restarted" || true'
echo "pushed roost to $HOST:~/roost"
