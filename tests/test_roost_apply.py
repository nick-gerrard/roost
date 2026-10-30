"""Run bootstrap/roost-apply against a fake root inside an ubuntu:24.04 container (GNU userland,
like production). Skipped when the docker daemon is unreachable. Set ROOST_APPLY_NATIVE=1 to run
the same cases on the host instead (BSD userland on macOS: only useful for debugging the harness)."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import REPO, stage

DOCKER = shutil.which("docker") or "/opt/homebrew/bin/docker"
CASES = Path(__file__).with_name("apply_cases.sh")
NATIVE = os.environ.get("ROOST_APPLY_NATIVE") == "1"
CASE_NAMES = re.findall(r"^case_(\w+)\(\)", CASES.read_text(), re.M)


def docker_up() -> bool:
    try:
        return subprocess.run([DOCKER, "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _prepare(dirs, work: Path, home_target: str, apps_target: str):
    """Render the real templates for both examples and rewrite the host paths to target paths."""
    home, apps = dirs
    out = work / "staged"
    for name, pins in (("milliways", dict(pin_backend=8002, pin_frontend=3002)),
                       ("pwhl", dict(pin_backend=8000))):
        staged, _ = stage(dirs, name, pins)
        shutil.copytree(staged, out / name)
        for f in (out / name).iterdir():
            if f.is_file():
                text = f.read_text().replace(str(home), home_target).replace(str(apps), apps_target)
                f.write_text(text)


@pytest.fixture(scope="session")
def apply_results(tmp_path_factory):
    if NATIVE:
        pass
    elif not docker_up():
        pytest.skip("docker daemon not reachable (start colima/Docker Desktop to run roost-apply tests)")
    # colima/Docker Desktop only share $HOME by default, so keep the mounted dir inside the repo
    base = Path(tempfile.mkdtemp(prefix=".apply-", dir=REPO)) if not NATIVE else tmp_path_factory.mktemp("apply")
    work = base / "w"
    work.mkdir()
    # reuse the `dirs` layout without a function-scoped fixture
    mp = pytest.MonkeyPatch()
    from roostd import deployer as dm
    home, apps = base / "h", base / "a"
    mp.setattr(dm, "ROOST_HOME", home)
    mp.setattr(dm, "APPS_ROOT", apps)
    mp.setattr(dm, "RUN_USER", "nickg" if not NATIVE else os.environ.get("USER", "nickg"))
    try:
        if NATIVE:
            env = dict(os.environ, NATIVE="1", SRC=str(REPO), W=str(work), FAKE=str(base / "fake"),
                       TEST_HOME=str(base / "nhome" / ".roost"), TEST_APPS=str(base / "nhome"),
                       RUSER=os.environ.get("USER", "nickg"))
            _prepare((home, apps), work, env["TEST_HOME"], env["TEST_APPS"])
            cmd = ["bash", str(CASES)]
        else:
            env = None
            _prepare((home, apps), work, "/home/nickg/.roost", "/home/nickg")
            shutil.copy(CASES, work / "apply_cases.sh")
            cmd = [DOCKER, "run", "--rm", "-v", f"{REPO}:/src:ro", "-v", f"{work}:/w",
                   "-e", "SRC=/src", "-e", "W=/w", "-e", "FAKE=/fakeroot", "-e", "RUSER=nickg",
                   "-e", "TEST_HOME=/home/nickg/.roost", "-e", "TEST_APPS=/home/nickg",
                   "ubuntu:24.04", "bash", "-c",
                   "useradd -m nickg && bash /w/apply_cases.sh"]
    finally:
        mp.undo()
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    finally:
        if not NATIVE:
            shutil.rmtree(base, ignore_errors=True)
    results, detail, cur = {}, {}, None
    for line in p.stdout.splitlines():
        m = re.match(r"RESULT (\w+) (PASS|FAIL)", line)
        if m:
            cur = m.group(1)
            results[cur] = m.group(2) == "PASS"
            detail[cur] = []
        elif cur:
            detail[cur].append(line)
    if not results:
        pytest.fail(f"no results from harness:\n{p.stdout}\n{p.stderr}")
    return results, detail


@pytest.mark.parametrize("case", CASE_NAMES)
def test_roost_apply(case, apply_results):
    results, detail = apply_results
    assert case in results, "case did not run"
    assert results[case], "\n".join(detail[case])
