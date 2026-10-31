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


GO_BE = '[backend]\nruntime = "go"\nbuild = "go build -o app ."\nstart = "app"\n'


def test_go_runtime_parses():
    m = Manifest.parse('name = "a"\n' + GO_BE + '[backend.env]\nADDR = "127.0.0.1:{port}"\n')
    assert m.backend.runtime == "go" and m.backend.build == "go build -o app ."
    assert m.backend.env == {"ADDR": "127.0.0.1:{port}"}


def test_go_needs_build():
    with pytest.raises(ValueError, match="build"):
        Manifest.parse('name = "a"\n[backend]\nruntime = "go"\nstart = "app --port {port}"\n')


def test_unknown_runtime():
    with pytest.raises(ValueError, match="runtime"):
        Manifest.parse('name = "a"\n[backend]\nruntime = "ruby"\nstart = "x {port}"\n')


def test_python_build_optional_and_parsed():
    m = Manifest.parse('name = "a"\n[backend]\nstart = "x {port}"\nbuild = "npm ci && npm run build:css"\n')
    assert m.backend.runtime == "python-uv" and m.backend.build == "npm ci && npm run build:css"


@pytest.mark.parametrize("key", ["lower", "1ABC", "A-B", "A B"])
def test_env_bad_key(key):
    with pytest.raises(ValueError, match="backend.env"):
        Manifest.parse(f'name = "a"\n' + GOOD_BE + f'[backend.env]\n"{key}" = "v"\n')


@pytest.mark.parametrize("val", ['a\\nb', 'a\\"b', 'a\\\\b', 'a%b', 'a\\rb'])
def test_env_bad_value(val):
    with pytest.raises(ValueError, match="backend.env"):
        Manifest.parse('name = "a"\n' + GOOD_BE + f'[backend.env]\nK = "{val}"\n')


def test_env_non_string_value():
    with pytest.raises(ValueError, match="backend.env"):
        Manifest.parse('name = "a"\n' + GOOD_BE + '[backend.env]\nK = 5\n')


def test_env_not_a_table():
    with pytest.raises(ValueError, match="table"):
        Manifest.parse('name = "a"\n[backend]\nstart = "x {port}"\nenv = "K=V"\n')


def test_env_good_keys():
    m = Manifest.parse('name = "a"\n' + GOOD_BE + '[backend.env]\n_X1 = "v"\nSCRUPLES_ADDR = "a b"\n')
    assert set(m.backend.env) == {"_X1", "SCRUPLES_ADDR"}


def test_port_in_env_only_ok():
    m = Manifest.parse('name = "a"\n[backend]\nstart = "app"\n[backend.env]\nADDR = ":{port}"\n')
    assert m.backend.start == "app"


def test_port_nowhere_rejected():
    with pytest.raises(ValueError, match="port"):
        Manifest.parse('name = "a"\n[backend]\nstart = "app"\n[backend.env]\nADDR = ":80"\n')
