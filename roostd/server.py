"""roostd HTTP surface: GitHub webhook, manual deploy, env push, logs.

    ROOST_SECRET=... uvicorn roostd.server:app --host 127.0.0.1 --port 8900

nginx exposes it at https://roost.nickgerrard.dev (see bootstrap/install.sh).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from . import history as hist
from .deployer import APPS_ROOT, ROOST_HOME, Deployer
from .manifest import NAME_RE
from .registry import Registry

SECRET = os.environ["ROOST_SECRET"].encode()  # webhook HMAC key + API bearer token
OWNER = os.environ.get("ROOST_GITHUB_OWNER", "nick-gerrard")  # only deploy repos we own
RESERVED_PORTS = {int(p) for p in os.environ.get("ROOST_RESERVED_PORTS", "").split(",") if p}
LOG_DIR = ROOST_HOME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")

app = FastAPI(title="roostd")
registry = Registry(ROOST_HOME / "registry.json", reserved=RESERVED_PORTS)
deployer = Deployer(registry)
history = hist.History(ROOST_HOME / "history.jsonl")


def _logger(repo_name: str):
    path = LOG_DIR / f"{repo_name}.log"
    def log(msg: str) -> None:
        with path.open("a") as f:
            f.write(f"{datetime.now():%H:%M:%S} {msg}\n")
    return log


def _commit(repo_name: str) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=APPS_ROOT / repo_name,
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _run_deploy(repo_name: str, repo_url: str, ref: str, trigger: str) -> None:
    log = _logger(repo_name)
    log(f"=== {datetime.now():%Y-%m-%d} deploy {repo_name}@{ref} (trigger={trigger}) ===")
    entry = {"repo": repo_name, "ref": ref, "trigger": trigger, "started": hist.now()}
    hist.running[repo_name] = entry
    try:
        rec = deployer.deploy(repo_name, repo_url, ref, trigger, log)
        entry["result"] = "ok" if rec else "skipped"
        if rec:
            entry.update(app=rec.name, commit=_commit(repo_name))
    except Exception as e:  # noqa: BLE001 — log every failure, never crash the daemon
        log(f"❌ DEPLOY FAILED: {e}")
        entry.update(result="failed", error=str(e)[:300])
    finally:
        entry["finished"] = hist.now()
        hist.running.pop(repo_name, None)
        history.append(entry)


def _verify_bearer(authorization: str | None) -> None:
    expected = f"Bearer {SECRET.decode()}"
    if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "bad token")


def _lookup(name: str):
    rec = registry.get(name) or registry.by_repo(name)
    if not rec:
        raise HTTPException(404, f"unknown app {name!r}")
    return rec


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/status")
def status(authorization: str | None = Header(None)):
    _verify_bearer(authorization)
    return {n: vars(r) for n, r in registry.apps.items()}


@app.get("/logs/{name}")
def logs(name: str, authorization: str | None = Header(None), tail: int = 200):
    _verify_bearer(authorization)
    repo = registry.get(name).repo_name if registry.get(name) else name
    if not REPO_RE.match(repo):
        raise HTTPException(400, "bad name")
    path = LOG_DIR / f"{repo}.log"
    if not path.exists():
        raise HTTPException(404, "no logs")
    return {"log": "\n".join(path.read_text().splitlines()[-tail:])}


@app.post("/hooks/github")
async def github_hook(request: Request, background: BackgroundTasks,
                      x_hub_signature_256: str | None = Header(None),
                      x_github_event: str | None = Header(None)):
    body = await request.body()
    expected = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    if not x_hub_signature_256 or not hmac.compare_digest(x_hub_signature_256.encode(), expected.encode()):
        raise HTTPException(401, "bad signature")

    payload = json.loads(body)
    if x_github_event == "ping":
        return {"pong": True}

    repo = payload.get("repository") or {}
    if (repo.get("owner") or {}).get("login") != OWNER or not REPO_RE.match(repo.get("name", "")):
        raise HTTPException(403, "repo not allowed")
    if repo.get("private"):
        return {"skipped": "private repos need a clone token (not configured)"}
    repo_name, repo_url = repo["name"], repo["clone_url"]

    if x_github_event == "push":
        if payload.get("ref") != "refs/heads/main" or payload.get("deleted"):
            return {"skipped": f"ref {payload.get('ref')}"}
        background.add_task(_run_deploy, repo_name, repo_url, "main", "push")
        return {"queued": repo_name, "trigger": "push"}

    if x_github_event == "workflow_run":
        run = payload.get("workflow_run") or {}
        if (payload.get("action") == "completed" and run.get("conclusion") == "success"
                and run.get("head_branch") == "main" and run.get("event") == "push"):
            background.add_task(_run_deploy, repo_name, repo_url, "main", "ci-pass")
            return {"queued": repo_name, "trigger": "ci-pass"}
        return {"skipped": f"workflow_run {payload.get('action')}/{run.get('conclusion')}"}

    return {"ignored": x_github_event}


@app.post("/deploy/{name}")
def manual_deploy(name: str, background: BackgroundTasks, ref: str = "main",
                  authorization: str | None = Header(None)):
    _verify_bearer(authorization)
    if not REF_RE.match(ref):
        raise HTTPException(400, "bad ref")
    rec = _lookup(name)
    background.add_task(_run_deploy, rec.repo_name, rec.repo_url, ref, "manual")
    return {"queued": rec.name, "ref": ref}


@app.post("/env/{name}")
async def push_env(name: str, request: Request, kind: str = "",
                   authorization: str | None = Header(None)):
    """Upload an env file. kind: '' (runtime, backend), 'frontend', or 'build'."""
    _verify_bearer(authorization)
    if not NAME_RE.match(name) or kind not in ("", "frontend", "build"):
        raise HTTPException(400, "bad name/kind")
    body = (await request.body()).decode()
    path = deployer.env_path(name, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(body)
    keys = [l.split("=", 1)[0] for l in body.splitlines() if "=" in l and not l.startswith("#")]
    return {"wrote": str(path), "keys": keys, "note": "redeploy to apply"}


history.backfill_from_logs(LOG_DIR)

from .dashboard import router as dashboard_router  # noqa: E402 — needs the globals above

app.include_router(dashboard_router)
