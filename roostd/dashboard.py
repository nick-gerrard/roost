"""Browser dashboard at / — app status, deploy history, logs, and a deploy button.

Login is the roost secret; the session cookie and CSRF token are HMACs of it, so
rotating ROOST_SECRET signs everyone out. No extra dependencies: forms are parsed
by hand so python-multipart isn't needed.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .deployer import DOMAIN

router = APIRouter()
COOKIE = "roost_session"
TEMPLATES = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
                        autoescape=select_autoescape(["html"]))
NGINX_ENABLED = Path("/etc/nginx/sites-enabled")


def _server():
    from . import server  # late import: server.py includes this router
    return server


def _mac(purpose: bytes) -> str:
    return hmac.new(_server().SECRET, purpose, hashlib.sha256).hexdigest()


def _authed(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), _mac(b"session-v1").encode())


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode(errors="replace")
    return {k: v[0] for k, v in parse_qs(body).items()}


def _render(name: str, status: int = 200, **ctx) -> HTMLResponse:
    return HTMLResponse(TEMPLATES.get_template(name).render(domain=DOMAIN, **ctx), status_code=status)


def _ago(iso: str | None) -> str:
    if not iso:
        return ""
    secs = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit} ago"
    return "just now"


def _duration(e: dict) -> str:
    if not e.get("finished"):
        return ""
    s = int((datetime.fromisoformat(e["finished"]) - datetime.fromisoformat(e["started"])).total_seconds())
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _unit_states(units: list[str]) -> dict[str, str]:
    if not units:
        return {}
    try:
        out = subprocess.run(["systemctl", "is-active", *units], capture_output=True,
                             text=True, timeout=3).stdout.split()
    except (OSError, subprocess.SubprocessError):
        out = []
    return {u: (out[i] if i < len(out) else "unknown") for i, u in enumerate(units)}


def _other_sites(managed: set[str]) -> list[str]:
    """Hostnames nginx serves that roost doesn't manage (read-only; for the overview)."""
    names: set[str] = set()
    try:
        for f in NGINX_ENABLED.iterdir():
            text = f.read_text(errors="replace")
            if text.startswith("# roost-managed"):
                continue
            names.update(n for m in re.findall(r"server_name\s+([^;]+);", text) for n in m.split())
    except OSError:
        return []
    return sorted(n for n in names - managed if n not in ("_", "localhost") and n != f"roost.{DOMAIN}")


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _authed(request):
        return _render("login.html")
    s = _server()
    last = s.history.last_by_repo()
    apps = []
    for rec in sorted(s.registry.apps.values(), key=lambda r: r.name):
        units = [f"{rec.name}-api"] if rec.backend_port else []
        if rec.frontend_port:
            units.append(f"{rec.name}-frontend")
        e = s.hist.running.get(rec.repo_name) or last.get(rec.repo_name)
        apps.append(dict(
            rec=rec, url=f"https://{rec.subdomain}.{DOMAIN}" if rec.subdomain else None,
            units=_unit_states(units), last=e, running=rec.repo_name in s.hist.running,
            ago=_ago(e and (e.get("finished") or e["started"])), duration=e and _duration(e),
        ))
    recent = [dict(e, ago=_ago(e.get("finished") or e["started"]), duration=_duration(e))
              for e in s.history.recent(15)]
    managed = {f"{r.subdomain}.{DOMAIN}" for r in s.registry.apps.values() if r.subdomain}
    return _render("dashboard.html", apps=apps, recent=recent, others=_other_sites(managed),
                   csrf=_mac(b"csrf-v1"), refreshing=bool(s.hist.running))


@router.post("/login")
async def login(request: Request):
    form = await _form(request)
    if not hmac.compare_digest(form.get("password", "").encode(), _server().SECRET):
        return _render("login.html", status=401, error=True)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _mac(b"session-v1"), max_age=30 * 86400,
                    httponly=True, secure=True, samesite="strict")
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.post("/ui/deploy/{name}")
async def ui_deploy(name: str, request: Request, background: BackgroundTasks):
    form = await _form(request)
    if not _authed(request) or not hmac.compare_digest(form.get("csrf", "").encode(),
                                                       _mac(b"csrf-v1").encode()):
        raise HTTPException(403, "forbidden")
    s = _server()
    rec = s._lookup(name)
    if rec.repo_name not in s.hist.running:
        background.add_task(s._run_deploy, rec.repo_name, rec.repo_url, "main", "manual")
        # mark it running now so the redirected page already shows it
        s.hist.running[rec.repo_name] = {"repo": rec.repo_name, "ref": "main",
                                         "trigger": "manual", "started": s.hist.now()}
    return RedirectResponse("/", status_code=303)


@router.get("/ui/logs/{name}", response_class=HTMLResponse)
def ui_logs(name: str, request: Request, lines: int = 300):
    if not _authed(request):
        return RedirectResponse("/", status_code=303)
    s = _server()
    rec = s._lookup(name)
    path = s.LOG_DIR / f"{rec.repo_name}.log"
    text = "\n".join(path.read_text().splitlines()[-min(lines, 5000):]) if path.exists() else ""
    return _render("logs.html", rec=rec, log=text, lines=lines,
                   running=rec.repo_name in s.hist.running)
