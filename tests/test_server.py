import hashlib
import hmac
import importlib
import json
import stat

import pytest
from fastapi.testclient import TestClient

SECRET = "s3cret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOST_SECRET", SECRET)
    monkeypatch.setenv("ROOST_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ROOST_GITHUB_OWNER", "nick-gerrard")
    import roostd.deployer
    import roostd.server
    importlib.reload(roostd.deployer)
    mod = importlib.reload(roostd.server)
    mod.queued = []
    monkeypatch.setattr(mod, "_run_deploy", lambda *a: mod.queued.append(a))
    mod.client = TestClient(mod.app)
    yield mod
    # leave the modules reloaded against the default env for later tests
    monkeypatch.undo()
    importlib.reload(roostd.deployer)


def hook(srv, event, payload, secret=SECRET, sig=None):
    body = json.dumps(payload).encode()
    sig = sig or "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return srv.client.post("/hooks/github", content=body, headers={
        "X-Hub-Signature-256": sig, "X-GitHub-Event": event, "Content-Type": "application/json"})


def repo(owner="nick-gerrard", name="pwhl", private=False):
    return {"owner": {"login": owner}, "name": name, "private": private,
            "clone_url": f"https://github.com/{owner}/{name}.git"}


def test_bad_signature(srv):
    assert hook(srv, "push", {}, secret="wrong").status_code == 401
    assert hook(srv, "push", {}, sig="sha256=00").status_code == 401
    r = srv.client.post("/hooks/github", content=b"{}", headers={"X-GitHub-Event": "push"})
    assert r.status_code == 401


def test_ping(srv):
    r = hook(srv, "ping", {"zen": "x"})
    assert r.status_code == 200 and r.json() == {"pong": True}


def test_push_non_main_skipped(srv):
    r = hook(srv, "push", {"ref": "refs/heads/dev", "repository": repo()})
    assert "skipped" in r.json() and srv.queued == []


def test_push_deleted_skipped(srv):
    r = hook(srv, "push", {"ref": "refs/heads/main", "deleted": True, "repository": repo()})
    assert "skipped" in r.json() and srv.queued == []


def test_wrong_owner(srv):
    r = hook(srv, "push", {"ref": "refs/heads/main", "repository": repo(owner="evil")})
    assert r.status_code == 403 and srv.queued == []


def test_bad_repo_name(srv):
    r = hook(srv, "push", {"ref": "refs/heads/main", "repository": repo(name="a b;rm")})
    assert r.status_code == 403


def test_private_repo_skipped(srv):
    r = hook(srv, "push", {"ref": "refs/heads/main", "repository": repo(private=True)})
    assert "skipped" in r.json() and srv.queued == []


def test_push_main_queues(srv):
    r = hook(srv, "push", {"ref": "refs/heads/main", "repository": repo()})
    assert r.json() == {"queued": "pwhl", "trigger": "push"}
    assert srv.queued == [("pwhl", "https://github.com/nick-gerrard/pwhl.git", "main", "push")]


def wf(conclusion="success", branch="main", event="push", action="completed"):
    return {"action": action, "repository": repo(),
            "workflow_run": {"conclusion": conclusion, "head_branch": branch, "event": event}}


def test_workflow_run_success_queues_ci_pass(srv):
    r = hook(srv, "workflow_run", wf())
    assert r.json() == {"queued": "pwhl", "trigger": "ci-pass"}
    assert srv.queued[0][3] == "ci-pass"


@pytest.mark.parametrize("kw", [dict(conclusion="failure"), dict(branch="dev"),
                                dict(event="pull_request"), dict(action="requested")])
def test_workflow_run_not_deployable(srv, kw):
    r = hook(srv, "workflow_run", wf(**kw))
    assert "skipped" in r.json() and srv.queued == []


def test_unknown_event_ignored(srv):
    assert "ignored" in hook(srv, "issues", {"repository": repo()}).json()


def test_status_requires_bearer(srv):
    assert srv.client.get("/status").status_code == 401
    assert srv.client.get("/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert srv.client.get("/status", headers={"Authorization": f"Bearer {SECRET}"}).json() == {}


def test_non_ascii_bearer_is_401_not_500(srv):
    r = srv.client.get("/status", headers={"Authorization": "Bearer café".encode("latin-1")})
    assert r.status_code == 401


def test_healthz_open(srv):
    assert srv.client.get("/healthz").json() == {"ok": True}


def test_env_write_mode_0600(srv, tmp_path):
    assert srv.client.post("/env/pwhl", content=b"A=1\n").status_code == 401
    r = srv.client.post("/env/pwhl", content=b"# c\nA=1\nB='x=y'\n", headers=AUTH)
    assert r.status_code == 200 and r.json()["keys"] == ["A", "B"]
    path = tmp_path / "home" / "env" / "pwhl.env"
    assert path.read_text() == "# c\nA=1\nB='x=y'\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_env_kinds(srv, tmp_path):
    for kind in ("frontend", "build"):
        assert srv.client.post(f"/env/pwhl?kind={kind}", content=b"A=1", headers=AUTH).status_code == 200
        assert (tmp_path / "home" / "env" / f"pwhl.{kind}.env").exists()


def test_env_rewrite_keeps_0600(srv, tmp_path):
    path = tmp_path / "home" / "env" / "pwhl.env"
    srv.client.post("/env/pwhl", content=b"A=1", headers=AUTH)
    path.chmod(0o644)
    srv.client.post("/env/pwhl", content=b"A=2", headers=AUTH)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("url", ["/env/pwhl?kind=bogus", "/env/Bad_Name", "/env/pwhl?kind=..%2Fx"])
def test_env_rejects_bad_input(srv, url):
    assert srv.client.post(url, content=b"A=1", headers=AUTH).status_code in (400, 404)


def test_manual_deploy(srv):
    srv.registry.ensure(name="pwhl", repo_name="pwhl", repo_url="u", subdomain="pwhl",
                        on="push", need_backend=True, need_frontend=False)
    assert srv.client.post("/deploy/pwhl").status_code == 401
    assert srv.client.post("/deploy/pwhl?ref=-bad", headers=AUTH).status_code == 400
    assert srv.client.post("/deploy/nope", headers=AUTH).status_code == 404
    r = srv.client.post("/deploy/pwhl?ref=dev", headers=AUTH)
    assert r.json() == {"queued": "pwhl", "ref": "dev"}
    assert srv.queued == [("pwhl", "u", "dev", "manual")]


def test_logs(srv):
    assert srv.client.get("/logs/pwhl", headers=AUTH).status_code == 404
    srv._logger("pwhl")("hello")
    assert "hello" in srv.client.get("/logs/pwhl", headers=AUTH).json()["log"]
