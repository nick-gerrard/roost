import importlib
import json

import pytest
from fastapi.testclient import TestClient

SECRET = "s3cret"


@pytest.fixture
def srv(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "pwhl-stats.log").write_text(
        "23:53:57 === 2026-10-30 deploy pwhl-stats@roost (trigger=manual) ===\n"
        "23:54:10   building...\n"
        "23:54:33 ✅ pwhl deployed -> https://pwhl.nickgerrard.dev\n"
        "23:55:55 === 2026-10-30 deploy pwhl-stats@main (trigger=push) ===\n"
        "23:55:58 skipped: trigger 'push' but manifest wants on='ci-pass'\n")
    (home / "registry.json").write_text(json.dumps({
        "pwhl": {"name": "pwhl", "repo_name": "pwhl-stats",
                 "repo_url": "https://github.com/nick-gerrard/pwhl-stats.git",
                 "subdomain": "pwhl", "on": "ci-pass", "backend_port": 8000, "frontend_port": None}}))
    monkeypatch.setenv("ROOST_SECRET", SECRET)
    monkeypatch.setenv("ROOST_HOME", str(home))
    import roostd.dashboard
    import roostd.deployer
    import roostd.server
    importlib.reload(roostd.deployer)
    mod = importlib.reload(roostd.server)
    importlib.reload(roostd.dashboard)
    mod.queued = []
    monkeypatch.setattr(mod, "_run_deploy", lambda *a: mod.queued.append(a))
    monkeypatch.setattr(roostd.dashboard, "_unit_states", lambda units: {u: "active" for u in units})
    # Secure cookies are only sent over https
    mod.client = TestClient(mod.app, base_url="https://testserver")
    yield mod
    monkeypatch.undo()
    importlib.reload(roostd.deployer)


def login(srv, pw=SECRET):
    return srv.client.post("/login", content=f"password={pw}",
                           headers={"Content-Type": "application/x-www-form-urlencoded"},
                           follow_redirects=False)


def csrf(srv):
    from roostd.dashboard import _mac
    return _mac(b"csrf-v1")


def test_logged_out_shows_login(srv):
    r = srv.client.get("/")
    assert r.status_code == 200 and 'action="/login"' in r.text and "pwhl" not in r.text


def test_wrong_password(srv):
    r = login(srv, "nope")
    assert r.status_code == 401 and "That's not it" in r.text
    assert "roost_session" not in r.cookies


def test_login_sets_strict_secure_cookie(srv):
    r = login(srv)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_dashboard_lists_apps_and_backfilled_history(srv):
    login(srv)
    r = srv.client.get("/")
    assert r.status_code == 200
    assert "pwhl.nickgerrard.dev" in r.text and "pwhl-api · active" in r.text
    assert "after CI" in r.text
    # backfilled from the text log: one ok, one skipped
    hist = srv.history.recent()
    assert [e["result"] for e in hist] == ["skipped", "ok"]
    assert hist[1]["started"] == "2026-10-30T23:53:57+00:00"


def test_deploy_requires_session_and_csrf(srv):
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert srv.client.post("/ui/deploy/pwhl", content=f"csrf={csrf(srv)}", headers=form).status_code == 403
    login(srv)
    assert srv.client.post("/ui/deploy/pwhl", content="csrf=bad", headers=form).status_code == 403
    r = srv.client.post("/ui/deploy/pwhl", content=f"csrf={csrf(srv)}", headers=form,
                        follow_redirects=False)
    assert r.status_code == 303
    assert srv.queued == [("pwhl-stats", "https://github.com/nick-gerrard/pwhl-stats.git", "main", "manual")]
    # a second click while running doesn't queue twice
    srv.client.post("/ui/deploy/pwhl", content=f"csrf={csrf(srv)}", headers=form)
    assert len(srv.queued) == 1
    assert "deploying…" in srv.client.get("/").text


def test_logs_page_escapes_output(srv):
    login(srv)
    (srv.LOG_DIR / "pwhl-stats.log").write_text("12:00:00 <script>alert(1)</script>\n")
    r = srv.client.get("/ui/logs/pwhl")
    assert r.status_code == 200 and "&lt;script&gt;alert(1)" in r.text and "<script>alert(1)" not in r.text


def test_logs_page_requires_login(srv):
    r = srv.client.get("/ui/logs/pwhl", follow_redirects=False)
    assert r.status_code == 303


def test_run_deploy_records_history(srv, monkeypatch):
    import roostd.server as real
    importlib.reload(real)  # undo the _run_deploy stub for this test
    monkeypatch.setattr(real.deployer, "deploy", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    real._run_deploy("pwhl-stats", "https://x", "main", "push")
    e = real.history.recent(1)[0]
    assert e["result"] == "failed" and "boom" in e["error"] and e["finished"]
    assert real.hist.running == {}
