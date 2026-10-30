"""Core deploy orchestration: fetch -> check trigger -> allocate -> build -> render -> apply.

Runs as the same unprivileged user the apps run as (ROOST_USER, `nickg` today).
The single privileged step is `sudo roost-apply <name> <subdomain|->`, which
validates and installs the staged files, issues a cert if needed, and reloads.
"""
from __future__ import annotations

import getpass
import os
import posixpath
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Callable

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .manifest import Manifest
from .registry import AppRecord, Registry

ROOST_HOME = Path(os.environ.get("ROOST_HOME", Path.home() / ".roost"))
APPS_ROOT = Path(os.environ.get("ROOST_APPS_ROOT", Path.home()))
DOMAIN = os.environ.get("ROOST_DOMAIN", "nickgerrard.dev")
RUN_USER = os.environ.get("ROOST_USER", getpass.getuser())
APPLY_CMD = os.environ.get("ROOST_APPLY_CMD", "sudo -n /usr/local/sbin/roost-apply")
TEMPLATES = Path(__file__).parent / "templates"

Logger = Callable[[str], None]


class DeployError(RuntimeError):
    pass


class Skipped(Exception):
    pass


def _run(cmd: str, cwd: Path, log: Logger, env: dict | None = None) -> str:
    log(f"$ {cmd}  (in {cwd})")
    proc = subprocess.run(
        cmd, shell=True, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in (proc.stdout or "").splitlines():
        log(f"  {line}")
    if proc.returncode != 0:
        raise DeployError(f"command failed ({proc.returncode}): {cmd}")
    return proc.stdout or ""


def load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines the way systemd EnvironmentFile= does (quotes stripped)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v
    return env


class Deployer:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.jinja = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                                 trim_blocks=True, lstrip_blocks=True,
                                 undefined=StrictUndefined)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def env_path(self, name: str, kind: str = "") -> Path:
        suffix = f".{kind}" if kind else ""
        return ROOST_HOME / "env" / f"{name}{suffix}.env"

    def deploy(self, repo_name: str, repo_url: str, ref: str, trigger: str,
               log: Logger) -> AppRecord | None:
        with self._locks[repo_name]:
            try:
                return self._deploy(repo_name, repo_url, ref, trigger, log)
            except Skipped as s:
                log(f"skipped: {s}")
                return None

    def _deploy(self, repo_name: str, repo_url: str, ref: str, trigger: str,
                log: Logger) -> AppRecord:
        app_dir = APPS_ROOT / repo_name
        self._fetch(repo_url, ref, app_dir, log)

        # Read the manifest from the fetched commit *before* touching the
        # working tree, so a skipped trigger never leaves new code checked out.
        try:
            text = subprocess.run(
                ["git", "show", f"origin/{ref}:deploy.toml"], cwd=app_dir,
                capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            raise Skipped(f"{repo_name}@{ref} has no deploy.toml")
        m = Manifest.parse(text)
        if trigger != "manual" and trigger != m.on:
            raise Skipped(f"trigger {trigger!r} but manifest wants on={m.on!r}")

        rec = self.registry.ensure(
            name=m.name, repo_name=repo_name, repo_url=repo_url, subdomain=m.subdomain,
            on=m.on, need_backend=bool(m.backend),
            need_frontend=bool(m.frontend and m.frontend.kind == "node"),
            pin_backend=m.backend.port if m.backend else None,
            pin_frontend=m.frontend.port if m.frontend else None,
        )
        log(f"app={m.name} backend_port={rec.backend_port} "
            f"frontend_port={rec.frontend_port} subdomain={rec.subdomain}")

        _run(f"git checkout -q {shlex.quote(ref)}", app_dir, log)
        _run(f"git reset -q --hard origin/{shlex.quote(ref)}", app_dir, log)
        _run("git rev-parse --short HEAD", app_dir, log)

        if m.backend:
            self._build_backend(m, app_dir, log)
        if m.frontend:
            self._build_frontend(m, app_dir, rec, log)

        self._stage(m, app_dir, rec, log)
        _run(f"{APPLY_CMD} {m.name} {rec.subdomain or '-'}", ROOST_HOME, log)

        if m.backend and m.backend.health:
            self._health(rec.backend_port, m.backend.health, log)
        where = f"https://{rec.subdomain}.{DOMAIN}" if rec.subdomain else "(no site)"
        log(f"✅ {m.name} deployed -> {where}")
        return rec

    # ---- steps -----------------------------------------------------------

    def _fetch(self, repo_url: str, ref: str, app_dir: Path, log: Logger) -> None:
        if (app_dir / ".git").exists():
            _run(f"git fetch -q --prune origin {shlex.quote(ref)}", app_dir, log)
        else:
            app_dir.parent.mkdir(parents=True, exist_ok=True)
            _run(f"git clone -q {shlex.quote(repo_url)} {shlex.quote(str(app_dir))}",
                 app_dir.parent, log)

    def _build_backend(self, m: Manifest, app_dir: Path, log: Logger) -> None:
        be = m.backend
        if be.runtime != "python-uv":
            raise DeployError(f"unsupported backend runtime {be.runtime!r}")
        _run("uv sync", app_dir / be.project, log)
        app_env = load_env_file(self.env_path(m.name))
        workdir = app_dir / be.workdir
        if m.on == "push" and be.test:
            _run(f"uv run {be.test}", workdir, log, env=app_env)
        if be.migrate:
            _run(f"uv run {be.migrate}", workdir, log, env=app_env)

    def _build_frontend(self, m: Manifest, app_dir: Path, rec: AppRecord, log: Logger) -> None:
        fe = m.frontend
        build_env = {}
        if rec.subdomain:
            build_env["PUBLIC_API_URL"] = f"https://{rec.subdomain}.{DOMAIN}/api"
        if rec.backend_port:
            build_env["INTERNAL_API_URL"] = f"http://127.0.0.1:{rec.backend_port}"
        build_env.update(load_env_file(self.env_path(m.name, "build")))
        _run(fe.build, app_dir / fe.dir, log, env=build_env)

    def _stage(self, m: Manifest, app_dir: Path, rec: AppRecord, log: Logger) -> None:
        staged = ROOST_HOME / "staged" / m.name
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        fqdn = f"{rec.subdomain}.{DOMAIN}" if rec.subdomain else None
        ctx = dict(
            name=m.name, fqdn=fqdn, app_dir=str(app_dir), user=RUN_USER,
            backend=m.backend, frontend=m.frontend,
            backend_port=rec.backend_port, frontend_port=rec.frontend_port,
            env_file=str(self.env_path(m.name)),
            frontend_env_file=str(self.env_path(m.name, "frontend")),
        )
        if m.backend:
            ctx["start"] = m.backend.start.format(port=rec.backend_port)
            ctx["venv_bin"] = posixpath.normpath(f"{app_dir}/{m.backend.project}/.venv/bin")
            self._render("backend.service.j2", staged / f"{m.name}-api.service", ctx)
        if m.frontend and m.frontend.kind == "node":
            self._render("frontend.service.j2", staged / f"{m.name}-frontend.service", ctx)
        if m.frontend and m.frontend.kind == "static":
            shutil.copytree(app_dir / m.frontend.dir / m.frontend.output, staged / "www",
                            symlinks=False)
        if fqdn:
            self._render("nginx.conf.j2", staged / f"{m.name}.nginx.conf", {**ctx, "tls": True})
            self._render("nginx.conf.j2", staged / f"{m.name}.nginx.http.conf", {**ctx, "tls": False})
        log(f"staged {sorted(p.name for p in staged.iterdir())}")

    def _render(self, template: str, dest: Path, ctx: dict) -> None:
        dest.write_text(self.jinja.get_template(template).render(**ctx))

    def _health(self, port: int, path: str, log: Logger, tries: int = 15) -> None:
        url = f"http://127.0.0.1:{port}{path}"
        last: object = None
        for _ in range(tries):
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    log(f"health ok: {url} -> {r.status}")
                    return
            except Exception as e:  # noqa: BLE001 — retry on anything until timeout
                last = e
            time.sleep(1)
        raise DeployError(f"health check failed for {url}: {last!r}")
