# roost

A single-box PaaS for `*.nickgerrard.dev`. Pushing to main in an app repo
triggers a GitHub webhook to roostd on the Linode. roostd builds the app,
renders its systemd unit and nginx site, and a small root helper validates and
installs them. To deploy an app *with* roost, use the `roost` skill
(`~/.claude/skills/roost/`). This file is about changing roost itself.

## Layout

```
roostd/
  server.py      FastAPI: /hooks/github (HMAC), /deploy, /env, /logs, /status (bearer), /healthz
  dashboard.py   browser UI at / (cookie login = ROOST_SECRET, CSRF on POST)
  deployer.py    fetch → read manifest from origin/<ref> → trigger check → registry → checkout
                 → build (uv sync / build / test / migrate) → stage → sudo roost-apply → health
  manifest.py    deploy.toml schema; unknown keys rejected
  registry.py    apps + ports (+ optional checkout `dir`), JSON at ~/.roost/registry.json
  history.py     deploy history JSONL (+ one-time backfill from text logs)
  templates/     backend/frontend .service.j2, nginx.conf.j2, dashboard HTML
bootstrap/
  roost-apply    THE root boundary (bash). Validates staged files, backs up, installs, certs, restarts
  install.sh     one-time / re-runnable privileged setup (needs the user's sudo password)
  push.sh        laptop → server sync of ~/roost, uv sync, restart roostd (no sudo)
  roostd.service, sudoers.roostd, roost.conf
roostctl         laptop CLI (bash + curl + gh)
tests/           pytest; tests/apply_cases.sh runs roost-apply inside ubuntu:24.04 via docker
```

## Security model (keep it intact)

- roostd runs as `nickg`, the same user as the apps. It may run exactly one
  thing as root: `/usr/local/sbin/roost-apply <name> <subdomain|->`.
- roost-apply treats everything in `~/.roost/staged/` as hostile. It snapshots
  the staged files into a root-only temp dir, then validates them:
  - **Units:** allow-list of directives, `User` must be ROOST_USER, `ExecStart`
    must be node or under APPS_ROOT, and `EnvironmentFile` must be under
    `~/.roost/env`.
  - **nginx:** every line must match an allow-list of what `nginx.conf.j2`
    emits. Beyond that, `server_name` must be the app's FQDN, cert paths must
    be that FQDN's, `root` must be `/var/www/<name>`, and `proxy_pass` must
    target 127.0.0.1. One directive per line.
  - **Refusals:** it won't clobber non-`# roost-managed` files unless
    root-owned `/etc/roost/adopt/<name>` exists, and it won't take a hostname
    another enabled site serves.
- **Changing what the templates emit means changing roost-apply's allow-list
  in the same commit**, plus a test case in `tests/apply_cases.sh`. Otherwise
  every deploy fails with "contains a directive roost does not generate".
- Webhooks are accepted only for repos owned by `nick-gerrard` and only when
  public. API, logs, env and the dashboard all sit behind ROOST_SECRET.
- Root-side config (`/etc/roost/roost.conf`, adopt markers) is root-owned so
  that the unprivileged side can't widen what roost-apply accepts.

## Developing

```bash
uv sync
uv run pytest -q                   # unit tests; roost-apply container cases skip without docker
colima start && uv run pytest -q   # full suite incl. ~50 roost-apply cases (~2 min); colima stop after
```
- Tests reload `roostd.server`/`roostd.deployer` per test with env set first.
  Copy the fixtures in `tests/test_server.py` / `tests/test_dashboard.py`.
- `tests/test_deployer.py` runs real deploys against a local bare git repo,
  with stub `uv`/`npm`/`go`/apply scripts on PATH.
- `tests/apply_cases.sh` stubs `systemctl` (including `show -p NRestarts/SubState`
  sequences), `nginx`, `certbot`, `journalctl`, `openssl` and `rsync`, under
  `ROOST_TEST_ROOT`. Add a `case_*` function per behaviour.
- macOS is case-insensitive: don't write tests that rely on two paths differing
  only by case.

## Shipping changes

| changed | do |
|---|---|
| `roostd/*`, templates, `roostctl` | `uv run pytest -q`, commit, `git push`, then `bootstrap/push.sh` (syncs + restarts roostd; kills an in-flight deploy, so check the dashboard first) |
| `bootstrap/roost-apply`, `sudoers.roostd`, `roostd.service`, `roost.conf`, adopt markers in `install.sh` | all of the above, then the **user** runs `ssh -t linode 'sudo bash ~/roost/bootstrap/install.sh'` in their own terminal (`!` has no TTY so sudo can't prompt) |
| new required manifest field / behaviour | update `roostctl init`, `README.md`, and the skill's `reference/deploy-toml.md` |

After shipping, sanity-check: `curl https://roost.nickgerrard.dev/healthz`,
`roostctl status`, and redeploy one app (`roostctl deploy scruples` is cheap)
if the deploy path changed.

## Invariants and gotchas

- The manifest is read with `git show origin/<ref>:deploy.toml` **before**
  checkout, so a skipped trigger never changes the working tree.
- Deploys are serialized per repo (a lock in `Deployer`). `git reset --hard`
  only, never `git clean`: SQLite DBs, `.env` and `node_modules` live in the
  checkouts.
- Ports are stored per app and never recomputed. The pools are
  8003+/3003+, minus reserved and currently bound ports.
- The health check counts any non-5xx answer. roost-apply separately watches
  each restarted unit for `START_WAIT` (10s) and fails on auto-restart, a
  failed/dead state, or an NRestarts change. It then restores retired legacy
  units and sites.
- The Go toolchain is `/snap/go/current/bin/go`. `/snap/bin/go` is SIGTERM'd
  inside systemd services.
- The box has 1 GB RAM. Builds run in roostd's cgroup (MemoryMax 750M), so
  keep build steps lean.
- Deploy refs are branch names (`origin/<ref>`), not SHAs.
