"""Deploy history: an append-only JSONL file plus the set of deploys in flight."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
running: dict[str, dict] = {}  # repo_name -> entry for deploys currently in progress


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _result_of(line: str) -> str | None:
    msg = line[9:]
    if msg.startswith("✅"):
        return "ok"
    if msg.startswith("❌"):
        return "failed"
    if msg.startswith("skipped:"):
        return "skipped"
    return None


class History:
    def __init__(self, path: Path):
        self.path = path

    def append(self, entry: dict) -> None:
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(entry) + "\n")

    def recent(self, limit: int = 200) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-limit:]
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def backfill_from_logs(self, log_dir: Path) -> None:
        """One-time import of deploys that predate history.jsonl, parsed from the text logs."""
        if self.path.exists():
            return
        header = re.compile(r"^(\d\d:\d\d:\d\d) === (\d{4}-\d\d-\d\d) deploy (\S+)@(\S+) \(trigger=(\w[\w-]*)\) ===$")
        entries = []
        for log in sorted(log_dir.glob("*.log")):
            cur = None
            for line in log.read_text().splitlines():
                if m := header.match(line):
                    t, d, repo, ref, trigger = m.groups()
                    cur = {"repo": repo, "ref": ref, "trigger": trigger, "started": f"{d}T{t}+00:00"}
                elif cur and (res := _result_of(line)):
                    cur.update(result=res, finished=f"{cur['started'][:10]}T{line[:8]}+00:00")
                    entries.append(cur)
                    cur = None
        for e in sorted(entries, key=lambda e: e["started"]):
            self.append(e)

    def last_by_repo(self) -> dict[str, dict]:
        last: dict[str, dict] = {}
        for e in self.recent(1000):
            last.setdefault(e["repo"], e)
        return last
