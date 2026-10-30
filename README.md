# roost 🪺

A tiny single-box PaaS for `*.nickgerrard.dev`. Add a `deploy.toml` to a repo,
push to `main`, and the Linode clones it, assigns ports, writes the systemd
units and nginx site, gets a TLS cert, builds, migrates and restarts. No SSH.

```
push to main ─► GitHub webhook ─► roostd (roost.nickgerrard.dev, runs as nickg)
                                    1. git fetch, read deploy.toml from the new commit
                                    2. skip unless the trigger matches `on`
                                    3. allocate ports (registry.json), checkout
                                    4. uv sync / test / alembic, npm build
                                    5. render units + nginx into ~/.roost/staged/<app>
                                    6. sudo roost-apply <app> <sub>  ── the only root step:
                                         validate → back up → install → cert → restart → reload
                                    7. health check
```

## Adding a new site

1. `roostctl init > deploy.toml`, then edit it (reference below).
2. Push to `main`.
3. Run `roostctl webhook <repo>` once (or `roostctl webhook <repo> ci-pass`).
4. Run `roostctl deploy <app>` for the first deploy, or just push again.

DNS: the `*.nickgerrard.dev` wildcard record means a new subdomain already
resolves. On first deploy roost-apply serves HTTP, runs `certbot --nginx` for
that hostname, and then switches to HTTPS.

Secrets: `roostctl env push <app> .env.prod` for the backend, and
`roostctl env push <app> file frontend|build` for the node runtime or build-time
vars. They're stored in `~/.roost/env/` (0600) and never touch git.

## deploy.toml

```toml
name = "myapp"            # [a-z0-9-]; units are myapp-api / myapp-frontend, site file myapp
subdomain = "myapp"       # https://myapp.nickgerrard.dev; omit for a background worker
on = "push"               # "push", or "ci-pass" to deploy when a GitHub Actions run on main succeeds

[backend]                 # python + uv
workdir = "backend"       # WorkingDirectory
project = "backend"       # where `uv sync` runs / .venv lives (default = workdir)
start = "uvicorn main:app --host 127.0.0.1 --port {port}"   # resolved inside .venv/bin
migrate = "alembic upgrade head"   # optional, `uv run` in workdir with the app's env
test = "pytest -q"                 # optional, on="push" only; failure aborts before restart
health = "/health"                 # optional, polled on the backend after restart
strip_prefix = true                # nginx /api/x -> backend /x (false keeps /api/x)
# port = 8010                      # optional pin; otherwise allocated from 8003+

[frontend]                # SvelteKit
kind = "static"           # "static" (adapter-static -> /var/www/<name>) or "node" (adapter-node)
dir = "frontend"
build = "npm ci && npm run build"
output = "build"
fallback = "/index.html"  # static only (pwhl uses /404.html)
```

Defaults injected at build time: `PUBLIC_API_URL=https://<sub>.nickgerrard.dev/api`,
`INTERNAL_API_URL=http://127.0.0.1:<backend_port>`. Override them with a
`build` env file. When there's a backend but no frontend, `/` proxies to the backend.

## roostctl

```
roostctl login                 # pulls the API secret over ssh into ~/.config/roost/config
roostctl status | deploy <app> [ref] | logs <app> | env push ... | webhook <repo> [mode] | init
```

## Safety

- Only repos owned by `nick-gerrard` are accepted. Webhooks are HMAC-verified,
  and the API uses a bearer token (the same secret).
- `roost-apply` copies the staged files into a root-only temp dir, then validates
  them before changing anything:
  - **Units:** the only directives allowed are `Description After User WorkingDirectory
    Environment EnvironmentFile ExecStart Restart RestartSec WantedBy`.
    `User` must be `nickg`, and `ExecStart` must be node or a path under `/home/nickg`.
  - **nginx:** `server_name` must be the app's own hostname. `ssl_certificate`,
    `ssl_dhparam`, `include`, `root` and `proxy_pass` targets are each restricted
    to an expected path or localhost, and dangerous directives are rejected.
  - **Hostnames:** a hostname already claimed by another enabled site is refused.
  - **Existing files:** a unit or site file that roost didn't write (no
    `# roost-managed` header) is never overwritten, unless root has created
    `/etc/roost/adopt/<app>`. That marker is one-shot.
  - **Backups:** every replaced file goes to `/var/backups/roost/<app>/<timestamp>/`.
    If `nginx -t` fails, the previous site file is restored.
- Honest caveat: build steps run your repo's code as `nickg`, who is a sudoer
  (with a password). This setup protects against mistakes and bad manifests.
  It is not a sandbox for untrusted code. Running roostd as a dedicated user is
  the next hardening step.

## Layout

```
roostd/        FastAPI daemon: server.py (HTTP), deployer.py, registry.py, manifest.py, templates/
roostctl       laptop CLI
bootstrap/     push.sh (laptop → server, no sudo) · install.sh (one-time sudo) · roost-apply · sudoers · units
tests/         pytest suite (roost-apply is tested in an ubuntu container)
```

Updating roost itself: `bootstrap/push.sh` (rsyncs the code, then `uv sync`, then restarts roostd).
If `roost-apply`, the sudoers file or the units change, re-run `install.sh`.

## Server state (Oct 2026)

| app | repo | ports | managed by |
|---|---|---|---|
| milliways | milliways | api 8002, node 3002 | roost (adopted) |
| pwhl | pwhl-stats | api 8000, static | roost (adopted, `ci-pass`) |
| ducksbucks | DucksBucks | 8001 | GitHub Actions (not yet migrated) |
| home-services (trmnl) | home_services | 5001 | GitHub Actions |
| glove | glove | 9009 | manual (Go binary, not supported yet) |
| nhl | nhlbracket | 3001 (service stopped; site redirects to ducksbucks) | GitHub Actions |

Those ports are listed in `ROOST_RESERVED_PORTS` so the allocator never hands them out.

Optional upgrade: a wildcard *certificate* (DNS-01) would skip the per-site
certbot step. It needs a Porkbun API key on the server and the
`certbot-dns-porkbun` plugin. Per-site certs work fine, so this was left out.
