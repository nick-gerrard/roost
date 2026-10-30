import pytest

from roostd.manifest import Manifest
from conftest import read_example

GOOD_BE = '[backend]\nstart = "uvicorn m:app --port {port}"\n'


def test_parse_milliways():
    m = Manifest.parse(read_example("milliways"))
    assert (m.name, m.subdomain, m.on) == ("milliways", "milliways", "push")
    assert m.backend.workdir == "backend" and m.backend.project == "."
    assert m.backend.strip_prefix is True
    assert m.frontend.kind == "node"


def test_parse_pwhl():
    m = Manifest.parse(read_example("pwhl"))
    assert m.on == "ci-pass"
    assert m.backend.project == "backend"  # defaults to workdir
    assert m.frontend.kind == "static" and m.frontend.fallback == "/404.html"


@pytest.mark.parametrize("name", ["Bad", "-x", "has space", "a_b", "", "x" * 42])
def test_bad_name(name):
    with pytest.raises(ValueError):
        Manifest.parse(f'name = "{name}"\n' + GOOD_BE)


def test_missing_name():
    with pytest.raises(ValueError):
        Manifest.parse(GOOD_BE)


def test_bad_subdomain():
    with pytest.raises(ValueError):
        Manifest.parse('name = "a"\nsubdomain = "A.b"\n' + GOOD_BE)


def test_unknown_backend_key():
    with pytest.raises(ValueError, match="unknown keys"):
        Manifest.parse('name = "a"\n[backend]\nstart = "x {port}"\ndir = "backend"\n')


def test_unknown_frontend_key():
    with pytest.raises(ValueError, match="unknown keys"):
        Manifest.parse('name = "a"\n[frontend]\nkind = "node"\nbogus = 1\n')


def test_static_needs_subdomain():
    with pytest.raises(ValueError, match="subdomain"):
        Manifest.parse('name = "a"\n[frontend]\nkind = "static"\n')


def test_start_needs_port():
    with pytest.raises(ValueError, match="port"):
        Manifest.parse('name = "a"\n[backend]\nstart = "uvicorn m:app"\n')


@pytest.mark.parametrize("on", ["manual", "PUSH", "never"])
def test_bad_on(on):
    with pytest.raises(ValueError, match="on"):
        Manifest.parse(f'name = "a"\non = "{on}"\n' + GOOD_BE)


def test_needs_a_component():
    with pytest.raises(ValueError):
        Manifest.parse('name = "a"\n')


def test_bad_frontend_kind():
    with pytest.raises(ValueError, match="kind"):
        Manifest.parse('name = "a"\nsubdomain = "a"\n[frontend]\nkind = "spa"\n')
