import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from roostd import deployer as dmod
from roostd.registry import Registry

MANIFEST = textwrap.dedent("""\
    name = "pwhl"
    subdomain = "pwhl"
    on = "{on}"

    [backend]
    workdir = "backend"
    start = "uvicorn main:app --host 127.0.0.1 --port {{port}}"
    migrate = "alembic upgrade head"

    [frontend]
    kind = "static"
    fallback = "/404.html"
    """)


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(work: Path, msg: str, files: dict[str, str]):
    for rel, content in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    sh("git", "add", "-A", cwd=work)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg, cwd=work)
    sh("git", "push", "-q", "origin", "main", cwd=work)


@pytest.fixture
def env(tmp_path, monkeypatch, dirs):
    home, apps = dirs
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls.log"
    (stubs / "uv").write_text(
        f'#!/bin/sh\necho "uv $* DATABASE_URL=$DATABASE_URL PLAIN=$PLAIN pwd=$(pwd)" >> {calls}\n')
    (stubs / "npm").write_text(
        f'#!/bin/sh\necho "npm $*" >> {calls}\n'
        '[ "$1 $2" = "run build" ] && mkdir -p build && echo built > build/index.html\nexit 0\n')
    (stubs / "apply").write_text(f'#!/bin/sh\necho "apply $*" >> {calls}\n')
    for f in stubs.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    monkeypatch.setattr(dmod, "APPLY_CMD", str(stubs / "apply"))

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh("git", "clone", "-q", str(origin), str(work))
    sh("git", "checkout", "-q", "-b", "main", cwd=work)

    class E:
        pass

    e = E()
    e.home, e.apps, e.origin, e.work, e.calls = home, apps, origin, work, calls
    e.log = []
    e.dep = dmod.Deployer(Registry(home / "registry.json"))
    e.calls_text = lambda: calls.read_text() if calls.exists() else ""
    e.deploy = lambda trigger="ci-pass", ref="main": e.dep.deploy(
        "pwhl", str(origin), ref, trigger, e.log.append)
    return e


def seed(e, on="ci-pass", extra=None):
    commit(e.work, "init", {"deploy.toml": MANIFEST.format(on=on), "backend/main.py": "x=1\n",
                            "frontend/package.json": "{}", **(extra or {})})


def test_deploy_end_to_end(env):
    seed(env)
    rec = env.deploy()
    assert rec.backend_port == 8003
    assert (env.apps / "pwhl" / ".git").exists()
    staged = env.home / "staged" / "pwhl"
    assert {p.name for p in staged.iterdir()} == {
        "pwhl-api.service", "pwhl.nginx.conf", "pwhl.nginx.http.conf", "www"}
    assert (staged / "www" / "index.html").read_text().strip() == "built"
    calls = env.calls_text()
    assert "uv sync" in calls and "uv run alembic upgrade head" in calls
    assert "npm ci" in calls and "npm run build" in calls
    assert calls.strip().splitlines()[-1] == f"apply pwhl pwhl"
    assert (env.home / "registry.json").exists()


def test_redeploy_picks_up_new_commit(env):
    seed(env)
    env.deploy()
    commit(env.work, "two", {"backend/main.py": "x=2\n"})
    env.deploy()
    assert (env.apps / "pwhl" / "backend" / "main.py").read_text() == "x=2\n"
    assert env.calls_text().count("apply pwhl pwhl") == 2


def test_trigger_mismatch_skipped_and_head_unchanged(env):
    seed(env, on="ci-pass")
    env.deploy()
    head = sh("git", "rev-parse", "HEAD", cwd=env.apps / "pwhl")
    commit(env.work, "two", {"backend/main.py": "x=2\n"})
    before = env.calls_text()
    assert env.deploy(trigger="push") is None
    assert sh("git", "rev-parse", "HEAD", cwd=env.apps / "pwhl") == head
    assert (env.apps / "pwhl" / "backend" / "main.py").read_text() == "x=1\n"
    assert env.calls_text() == before  # nothing built, nothing applied
    assert any("skipped" in l for l in env.log)


def test_manual_trigger_always_runs(env):
    seed(env, on="ci-pass")
    assert env.deploy(trigger="manual") is not None


def test_missing_deploy_toml_skipped(env):
    commit(env.work, "init", {"README": "hi"})
    assert env.deploy() is None
    assert any("no deploy.toml" in l for l in env.log)
    assert env.calls_text() == ""


def test_env_file_values_passed_to_migrate(env):
    seed(env)
    p = env.home / "env" / "pwhl.env"
    p.parent.mkdir(parents=True)
    p.write_text('# c\nDATABASE_URL="postgres://u:p w@h/db"\nPLAIN=abc\n')
    env.deploy()
    mig = [l for l in env.calls_text().splitlines() if "alembic" in l][0]
    assert "DATABASE_URL=postgres://u:p w@h/db" in mig
    assert "PLAIN=abc" in mig
    assert mig.endswith("pwd=" + str((env.apps / "pwhl" / "backend").resolve())) or "backend" in mig


def test_failing_command_raises(env):
    seed(env)
    Path(env.calls.parent / "stubs" / "uv").write_text("#!/bin/sh\nexit 3\n")
    with pytest.raises(dmod.DeployError):
        env.deploy()
    assert "apply" not in env.calls_text()


def test_load_env_file_parsing(tmp_path):
    p = tmp_path / "e"
    p.write_text("A='x y'\nB=\"q\"\n  C = 1 \nbad\n#D=2\nE=\"unterminated\n")
    assert dmod.load_env_file(p) == {"A": "x y", "B": "q", "C": "1", "E": '"unterminated'}
    assert dmod.load_env_file(tmp_path / "missing") == {}
