import http.server
import os
import subprocess
import threading
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
        f'#!/bin/sh\necho "npm $* pwd=$(pwd)" >> {calls}\n'
        '[ "$1 $2" = "run build" ] && mkdir -p build && echo built > build/index.html\nexit 0\n')
    (stubs / "go").write_text(f'#!/bin/sh\necho "go $* pwd=$(pwd)" >> {calls}\n')
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


GO_MANIFEST = textwrap.dedent("""\
    name = "scruples"
    subdomain = "scruples"

    [backend]
    runtime = "go"
    workdir = "."
    project = "srv"
    build = "go build -o app ."
    start = "app"
    test = "go test ./..."
    migrate = "go run ./cmd/migrate"

    [backend.env]
    SCRUPLES_ADDR = "127.0.0.1:{port}"
    """)

PY_BUILD_MANIFEST = MANIFEST.format(on="push").replace(
    'migrate =', 'build = "npm ci && npm run build:css"\nmigrate =')


def deploy_repo(e, repo="scruples", trigger="manual"):
    return e.dep.deploy(repo, str(e.origin), "main", trigger, e.log.append)


def test_app_dir_honours_registry_override(env):
    commit(env.work, "init", {"deploy.toml": GO_MANIFEST, "srv/main.go": "package main\n"})
    env.dep.registry.ensure(name="scruples", repo_name="DucksBucks", repo_url="x", subdomain="scruples",
                            on="push", need_backend=True, need_frontend=False)
    env.dep.registry.apps["scruples"].dir = "legacy-ducks"
    assert env.dep.app_dir("DucksBucks") == env.apps / "legacy-ducks"
    assert env.dep.app_dir("other") == env.apps / "other"
    deploy_repo(env, "DucksBucks")
    assert (env.apps / "legacy-ducks" / ".git").exists()
    assert not (env.apps / "DucksBucks").exists()


def test_existing_checkout_origin_url_updated_before_fetch(env):
    seed(env)
    env.deploy()
    moved = env.origin.parent / "moved.git"
    env.origin.rename(moved)
    env.deploy_url = str(moved)
    commit_url = sh("git", "-C", str(env.apps / "pwhl"), "remote", "get-url", "origin")
    assert commit_url == str(env.origin)
    sh("git", "remote", "set-url", "origin", str(moved), cwd=env.work)
    commit(env.work, "two", {"backend/main.py": "x=2\n"})
    env.dep.deploy("pwhl", str(moved), "main", "ci-pass", env.log.append)  # fetch only works if set-url ran
    assert sh("git", "-C", str(env.apps / "pwhl"), "remote", "get-url", "origin") == str(moved)
    assert (env.apps / "pwhl" / "backend" / "main.py").read_text() == "x=2\n"
    cmds = [l for l in env.log if l.startswith("$ git")]
    assert cmds.index(next(c for c in cmds if "remote set-url" in c)) < \
        len(cmds) - 1 - cmds[::-1].index(next(c for c in cmds if "git fetch" in c))


def test_go_runtime_pipeline(env):
    commit(env.work, "init", {"deploy.toml": GO_MANIFEST, "srv/main.go": "package main\n"})
    rec = deploy_repo(env)
    calls = env.calls_text()
    assert "uv " not in calls
    app = env.apps / "scruples"
    lines = calls.splitlines()
    assert any(l.startswith("go build -o app .") and l.endswith(f"pwd={(app / 'srv').resolve()}") for l in lines)
    assert any(l.startswith("go test") and l.endswith(f"pwd={app.resolve()}") for l in lines)
    assert "uv run" not in calls
    assert any(l.startswith("go run ./cmd/migrate") and l.endswith(f"pwd={app.resolve()}") for l in lines)
    assert not any("uv run" in l for l in env.log)
    unit = (env.home / "staged" / "scruples" / "scruples-api.service").read_text()
    assert f"ExecStart={app}/srv/app\n" in unit and "/./" not in unit
    assert f"Environment=SCRUPLES_ADDR=127.0.0.1:{rec.backend_port}\n" in unit
    assert calls.strip().splitlines()[-1] == "apply scruples scruples"


def test_go_build_failure_aborts(env):
    commit(env.work, "init", {"deploy.toml": GO_MANIFEST, "srv/main.go": "package main\n"})
    (env.calls.parent / "stubs" / "go").write_text("#!/bin/sh\nexit 1\n")
    with pytest.raises(dmod.DeployError):
        deploy_repo(env)
    assert "apply" not in env.calls_text()


def test_python_build_runs_after_uv_sync(env):
    seed(env, on="push", extra={"deploy.toml": PY_BUILD_MANIFEST})
    env.deploy(trigger="push")
    lines = env.calls_text().splitlines()
    kinds = [l.split()[0] for l in lines]
    assert kinds.index("uv") < kinds.index("npm")
    sync = next(l for l in lines if l.startswith("uv sync"))
    assert lines.index(sync) < next(i for i, l in enumerate(lines) if l.startswith("npm run build:css"))
    assert any(l.startswith("npm run build:css") and "pwd=" + str((env.apps / "pwhl" / "backend").resolve()) in l
               for l in lines)
    assert any(l.startswith("uv run alembic upgrade head") for l in lines)


@pytest.fixture
def http_server():
    servers = []

    def start(code):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_address[1]

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


@pytest.mark.parametrize("code", [200, 301, 401, 404])
def test_health_ok_below_500(env, http_server, code):
    port = http_server(code)
    log = []
    env.dep._health(port, "/health", log.append, tries=1)
    assert any(f"health ok" in l and str(code) in l for l in log)


@pytest.mark.parametrize("code", [500, 503])
def test_health_5xx_fails(env, http_server, code, monkeypatch):
    monkeypatch.setattr(dmod.time, "sleep", lambda _: None)
    port = http_server(code)
    with pytest.raises(dmod.DeployError, match="health check failed"):
        env.dep._health(port, "/health", lambda _: None, tries=2)


def test_health_connection_refused_fails(env, monkeypatch):
    import socket
    monkeypatch.setattr(dmod.time, "sleep", lambda _: None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(dmod.DeployError):
        env.dep._health(port, "/", lambda _: None, tries=2)
