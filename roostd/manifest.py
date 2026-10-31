"""Parse a repo's deploy.toml into a typed Manifest."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
RUNTIMES = ("python-uv", "go")


@dataclass
class Backend:
    start: str                    # e.g. "uvicorn main:app --host 127.0.0.1 --port {port}"
    workdir: str = "backend"      # WorkingDirectory of the service
    project: str | None = None    # python-uv: where `uv sync` runs / .venv lives; go: module dir
    runtime: str = "python-uv"    # "python-uv" (start resolved in .venv/bin) | "go" (start resolved in project)
    build: str | None = None      # optional, run in project after deps (go: required, e.g. "go build -o app .")
    env: dict[str, str] = field(default_factory=dict)  # non-secret Environment=; {port} substituted
    migrate: str | None = None    # e.g. "alembic upgrade head" (python-uv: via `uv run`), in workdir
    test: str | None = None       # on="push" only; failure aborts before restart
    health: str | None = None     # backend-relative path, e.g. "/health"
    strip_prefix: bool = True     # nginx: /api/x -> backend /x (False keeps /api/x)
    port: int | None = None       # pin a port; otherwise allocated

    def __post_init__(self):
        if self.project is None:
            self.project = self.workdir


@dataclass
class Frontend:
    kind: str                     # "node" (adapter-node) | "static" (adapter-static)
    build: str = "npm ci && npm run build"
    dir: str = "frontend"
    output: str = "build"
    fallback: str = "/index.html"  # static only: try_files fallback (pwhl uses /404.html)
    port: int | None = None


@dataclass
class Manifest:
    name: str
    subdomain: str | None = None
    on: str = "push"              # "push" | "ci-pass"
    backend: Backend | None = None
    frontend: Frontend | None = None

    @classmethod
    def parse(cls, text: str) -> "Manifest":
        data = tomllib.loads(text)
        name = data.get("name")
        if not name or not NAME_RE.match(name):
            raise ValueError(f"deploy.toml: `name` must match {NAME_RE.pattern}")
        sub = data.get("subdomain")
        if sub is not None and not NAME_RE.match(sub):
            raise ValueError(f"deploy.toml: `subdomain` must match {NAME_RE.pattern}")
        on = data.get("on", "push")
        if on not in ("push", "ci-pass"):
            raise ValueError("deploy.toml: `on` must be push|ci-pass")

        backend = _build(Backend, data.get("backend"), "backend")
        frontend = _build(Frontend, data.get("frontend"), "frontend")
        if backend is None and frontend is None:
            raise ValueError("deploy.toml must define [backend] and/or [frontend]")
        if frontend and frontend.kind not in ("node", "static"):
            raise ValueError(f"frontend.kind must be node|static, got {frontend.kind!r}")
        if frontend and frontend.kind == "static" and not sub:
            raise ValueError("a static frontend needs a `subdomain`")
        if backend:
            _check_backend(backend)

        return cls(name=name, subdomain=sub, on=on, backend=backend, frontend=frontend)


def _check_backend(be: Backend) -> None:
    if be.runtime not in RUNTIMES:
        raise ValueError(f"backend.runtime must be one of {RUNTIMES}, got {be.runtime!r}")
    if be.runtime == "go" and not be.build:
        raise ValueError("a go backend needs `build` (e.g. \"go build -o app .\")")
    if not isinstance(be.env, dict):
        raise ValueError("backend.env must be a table")
    for k, v in be.env.items():
        if not ENV_KEY_RE.match(k) or not isinstance(v, str) or any(c in v for c in "\n\r\"\\%"):
            raise ValueError(f"backend.env: bad entry {k!r}")
    if "{port}" not in be.start and not any("{port}" in v for v in be.env.values()):
        raise ValueError("backend.start or backend.env must use {port}")


def _build(cls, table: dict | None, section: str):
    if table is None:
        return None
    known = {f.name for f in fields(cls)}
    unknown = set(table) - known
    if unknown:
        raise ValueError(f"deploy.toml [{section}]: unknown keys {sorted(unknown)}")
    return cls(**table)
