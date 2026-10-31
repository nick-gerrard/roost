from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO = EXAMPLES.parent


def read_example(name: str) -> str:
    return (EXAMPLES / f"{name}.deploy.toml").read_text()


import pytest  # noqa: E402

from roostd import deployer as deployer_mod  # noqa: E402
from roostd.manifest import Manifest  # noqa: E402
from roostd.registry import Registry  # noqa: E402


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    home, apps = tmp_path / "home", tmp_path / "apps"
    monkeypatch.setattr(deployer_mod, "ROOST_HOME", home)
    monkeypatch.setattr(deployer_mod, "APPS_ROOT", apps)
    monkeypatch.setattr(deployer_mod, "RUN_USER", "nickg")
    return home, apps


def stage(dirs, name, pins):
    """Stage an example manifest; APPS_ROOT is faked as /home/nickg in the rendered text."""
    home, apps = dirs
    m = Manifest.parse(read_example(name))
    app_dir = apps / name
    (app_dir / "frontend" / "build").mkdir(parents=True)
    (app_dir / "frontend" / "build" / "index.html").write_text("hi")
    reg = Registry(home / "registry.json")
    rec = reg.ensure(name=name, repo_name=name, repo_url="x", subdomain=m.subdomain, on=m.on,
                     need_backend=True, need_frontend=m.frontend.kind == "node", **pins)
    deployer_mod.Deployer(reg)._stage(m, app_dir, rec, lambda _: None)
    return home / "staged" / name, app_dir


GO_MANIFEST = """
name = "scruples"
subdomain = "scruples"

[backend]
runtime = "go"
workdir = "."
build = "go build -o app ."
start = "app"

[backend.env]
SCRUPLES_ADDR = "127.0.0.1:{port}"
OTHER = "static value"
"""


def stage_go(dirs, manifest=GO_MANIFEST, project_dir=""):
    home, apps = dirs
    m = Manifest.parse(manifest)
    app_dir = apps / "scruples"
    app_dir.mkdir(parents=True)
    reg = Registry(home / "registry.json")
    rec = reg.ensure(name="scruples", repo_name="scruples", repo_url="x", subdomain=m.subdomain,
                     on=m.on, need_backend=True, need_frontend=False)
    deployer_mod.Deployer(reg)._stage(m, app_dir, rec, lambda _: None)
    return home / "staged" / "scruples", str(app_dir), rec
