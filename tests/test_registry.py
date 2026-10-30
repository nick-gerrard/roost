import socket

import pytest

from roostd.registry import Registry


def ensure(reg, name, repo=None, sub=None, **kw):
    return reg.ensure(name=name, repo_name=repo or name, repo_url=f"https://x/{repo or name}",
                      subdomain=sub if sub is not None else name, on="push",
                      need_backend=kw.pop("be", True), need_frontend=kw.pop("fe", False), **kw)


@pytest.fixture
def reg(tmp_path):
    r = Registry(tmp_path / "reg.json", reserved={8003 + 1})
    ensure(r, "milliways", fe=True, pin_backend=8002, pin_frontend=3002)
    ensure(r, "pwhl", pin_backend=8000)
    return r


def test_seeded_ports_kept(reg):
    # re-ensuring without pins must not move existing ports
    m = ensure(reg, "milliways", fe=True)
    p = ensure(reg, "pwhl")
    assert (m.backend_port, m.frontend_port, p.backend_port) == (8002, 3002, 8000)


def test_new_app_skips_reserved_and_bound(reg):
    a = ensure(reg, "alpha")
    assert a.backend_port == 8003
    # 8004 reserved; bind 8005 for real
    with socket.socket() as s:
        s.bind(("127.0.0.1", 8005))
        s.listen()
        b = ensure(reg, "beta")
        assert b.backend_port == 8006
    c = ensure(reg, "gamma", fe=True)
    assert c.backend_port == 8005 and c.frontend_port == 3003


def test_subdomain_conflict(reg):
    with pytest.raises(ValueError, match="subdomain"):
        ensure(reg, "other", sub="pwhl")


def test_repo_conflict(reg):
    with pytest.raises(ValueError, match="repo"):
        ensure(reg, "other", repo="pwhl", sub="other")


def test_app_cannot_change_repo(reg):
    with pytest.raises(ValueError, match="belongs to repo"):
        ensure(reg, "pwhl", repo="elsewhere")


def test_failed_ensure_does_not_register_app(reg):
    with pytest.raises(ValueError):
        ensure(reg, "other", sub="pwhl")
    assert reg.get("other") is None


def test_persistence_roundtrip(reg, tmp_path):
    again = Registry(tmp_path / "reg.json")
    assert again.apps == reg.apps
    assert again.get("milliways").frontend_port == 3002
    assert again.by_repo("pwhl").name == "pwhl"
