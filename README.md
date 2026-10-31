# roost 🪺

A tiny single-box PaaS for `*.nickgerrard.dev`. Add a `deploy.toml` to a repo
and push to `main`. The Linode clones the repo and picks ports, writes the
systemd units and nginx site, and gets a TLS cert. Then it builds, migrates,
restarts and health-checks the app. No SSH, no hand-written config, no DNS
changes.

```
push to main ─► GitHub webhook ─► roostd (roost.nickgerrard.dev, runs as nickg)
   (or CI green: workflow_run)      1. git fetch; read deploy.toml from the new commit
                                    2. skip unless the trigger matches `on`
                                    3. allocate ports (registry.json), checkout
                                    4. uv sync / build / test / migrate (python-uv or go), frontend build
                                    5. render units + nginx into ~/.roost/staged/<app>
                                    6. sudo roost-apply <app> <sub>  ── the only root step:
                                         validate → back up → install → cert → restart
                                         → watch for crash loops → reload nginx
                                    7. health check
```

## Adding a site

```bash
roostctl init > deploy.toml          # edit it; reference below
git push                             # repo must be public under nick-gerrard
roostctl webhook <repo>              # or: roostctl webhook <repo> ci-pass
roostctl deploy <repo>               # first deploy (later pushes deploy themselves)
```

DNS is a `*.nickgerrard.dev` wildcard, so new subdomains already resolve. On
first deploy roost-apply serves HTTP while `certbot --nginx` issues the cert,
then switches to HTTPS.

Secrets never go in git. Upload them with `roostctl env push <app> prod.env`
(runtime), `... frontend` (node frontend runtime) or `... build`. They're
stored at `~/.roost/env/` with mode 0600.

## deploy.toml

```toml
name = "myapp"            # [a-z0-9-]; units myapp-api / myapp-frontend, site file myapp
subdomain = "myapp"       # https://myapp.nickgerrard.dev; omit for a background worker
on = "push"               # or "ci-pass": deploy when a GitHub Actions run on main succeeds

[backend]
runtime = "python-uv"     # or "go"
workdir = "backend"       # WorkingDirectory
project = "backend"       # python-uv: uv sync / .venv here; go: build here (default = workdir)
start = "uvicorn main:app --host 127.0.0.1 --port {port}"   # resolved in .venv/bin or project dir
build = "npm ci && npm run build:css"   # optional (required for go: "/snap/go/current/bin/go build -o app .")
migrate = "alembic upgrade head"        # optional, in workdir, with the app's env
test = "pytest -q"                      # optional, on="push" only; failure aborts before restart
health = "/health"                      # optional; any non-5xx answer counts
strip_prefix = true                     # with a frontend: nginx /api/x -> backend /x
# port = 8010                           # optional pin; otherwise allocated from 8003+

[backend.env]             # optional non-secret Environment= lines; {port} substituted
# PORT = "{port}"

[frontend]                # SvelteKit or any static build
kind = "static"           # "static" (-> /var/www/<name>) or "node" (adapter-node unit)
dir = "frontend"
build = "npm ci && npm run build"
output = "build"
fallback = "/index.html"  # static only; SvelteKit SPA with fallback: '404.html' -> "/404.html"
```

| shape | nginx `/api/*` | nginx `/` |
|---|---|---|
| backend + static frontend | backend (prefix stripped) | static files |
| backend + node frontend | backend (prefix stripped) | node frontend |
| backend only | — | backend, full path |
| static only | — | static files |

Frontend builds get `PUBLIC_API_URL=https://<sub>.nickgerrard.dev/api` and
`INTERNAL_API_URL=http://127.0.0.1:<backend_port>` by default.

## Operating

- **Dashboard:** https://roost.nickgerrard.dev. It shows every app's site,
  ports, unit state and last deploy, plus recent deploys, logs and a Deploy
  button. The password is ROOST_SECRET.
- **CLI:** `roostctl status | deploy <app> [branch] | logs <app> [n] | env push ... | webhook ... | init`.
  `roostctl login` fetches the secret over SSH.
- **Taking over a site that predates roost:** the root-owned marker
  `/etc/roost/adopt/<app>` (written by `install.sh`) allows a one-time takeover.
  It can list differently named legacy units and sites to retire (`unit x.service`,
  `site x.example`). They're backed up, retired, and restored automatically if
  the new unit doesn't come up or nginx rejects the config.

## Safety

- roostd runs as `nickg`. The only root action is `sudo roost-apply`, and
  roost-apply validates everything first:
  - **Units:** only a fixed set of directives is allowed, `User=nickg`,
    `ExecStart` must be under `/home/nickg` or be node, and env files must be
    roost's.
  - **nginx:** every line must match what the template generates, and only the
    app's own hostname, cert, web root and localhost upstreams are allowed.
  - **Existing files:** anything that isn't `# roost-managed` is never
    overwritten without an adopt marker. A hostname another site serves is
    refused.
  - **Backups:** every replaced file goes to `/var/backups/roost/<app>/<timestamp>/`.
- Webhooks are HMAC-verified and restricted to public repos owned by
  `nick-gerrard`. The API and dashboard need ROOST_SECRET.
- Honest caveat: builds run your repos' code as `nickg`, who is a sudoer (with
  a password). This protects against mistakes and bad manifests. It is not a
  sandbox for untrusted code.

## Layout and development

See [CLAUDE.md](CLAUDE.md) for the architecture, the security invariants, the
tests and how to ship changes. Short version: `uv run pytest -q`; with docker
(colima) running, the roost-apply cases run in an ubuntu:24.04 container. Ship
with `bootstrap/push.sh`. Re-run `bootstrap/install.sh` with sudo when
`roost-apply`, `sudoers` or the units change.

## Server (Oct 2026)

| app | repo | ports | deploys on |
|---|---|---|---|
| pwhl | pwhl-stats | api 8000, static | CI pass |
| ducksbucks | DucksBucks (`~/ducksbucks`) | 8001 | CI pass |
| milliways | milliways | api 8002, node 3002 | push |
| scruples | scruples | 8003 | push |
| home-services (trmnl), glove, nhl | — | 5001, 9009, 3001 | not on roost (reserved ports) |
