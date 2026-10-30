"""App registry + port allocator, persisted as JSON.

Ports are stored explicitly per app (not derived), so adopted apps keep the
ports they already run on. New apps get the lowest free port in each pool that
is not registered, not reserved (unmanaged services on the box), and not
currently bound.
"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

BACKEND_POOL = range(8003, 8100)
FRONTEND_POOL = range(3003, 3100)


@dataclass
class AppRecord:
    name: str
    repo_name: str                # GitHub repo name == checkout dir name
    repo_url: str
    subdomain: str | None = None
    on: str = "push"
    backend_port: int | None = None
    frontend_port: int | None = None


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class Registry:
    def __init__(self, path: str | Path, reserved: set[int] | None = None):
        self.path = Path(path)
        self.reserved = reserved or set()
        self._lock = threading.Lock()
        self.apps: dict[str, AppRecord] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.apps = {k: AppRecord(**v) for k, v in data.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self.apps.items()}, indent=2))
        tmp.replace(self.path)

    def get(self, name: str) -> AppRecord | None:
        return self.apps.get(name)

    def by_repo(self, repo_name: str) -> AppRecord | None:
        return next((a for a in self.apps.values() if a.repo_name == repo_name), None)

    def _used(self) -> set[int]:
        used = set(self.reserved)
        for a in self.apps.values():
            used.update(p for p in (a.backend_port, a.frontend_port) if p)
        return used

    def _alloc(self, pool: range, pinned: int | None) -> int:
        if pinned:
            return pinned
        used = self._used()
        for p in pool:
            if p not in used and _port_free(p):
                return p
        raise RuntimeError(f"no free port in {pool}")

    def ensure(self, *, name: str, repo_name: str, repo_url: str, subdomain: str | None,
               on: str, need_backend: bool, need_frontend: bool,
               pin_backend: int | None = None, pin_frontend: int | None = None) -> AppRecord:
        with self._lock:
            for other in self.apps.values():
                if other.name != name and subdomain and other.subdomain == subdomain:
                    raise ValueError(f"subdomain {subdomain!r} already belongs to {other.name!r}")
                if other.name != name and other.repo_name == repo_name:
                    raise ValueError(f"repo {repo_name!r} is already registered as {other.name!r}")

            rec = self.apps.get(name)
            if rec is None:
                rec = AppRecord(name=name, repo_name=repo_name, repo_url=repo_url)
                self.apps[name] = rec
            elif rec.repo_name != repo_name:
                raise ValueError(f"app {name!r} belongs to repo {rec.repo_name!r}, not {repo_name!r}")

            rec.repo_url, rec.subdomain, rec.on = repo_url, subdomain, on
            if need_backend and (rec.backend_port is None or pin_backend):
                rec.backend_port = self._alloc(BACKEND_POOL, pin_backend)
            if need_frontend and (rec.frontend_port is None or pin_frontend):
                rec.frontend_port = self._alloc(FRONTEND_POOL, pin_frontend)
            self._save()
            return rec
