import re

import pytest

from conftest import stage


@pytest.fixture
def milliways(dirs):
    staged, app_dir = stage(dirs, "milliways", dict(pin_backend=8002, pin_frontend=3002))
    return staged, str(app_dir)


@pytest.fixture
def pwhl(dirs):
    staged, app_dir = stage(dirs, "pwhl", dict(pin_backend=8000))
    return staged, str(app_dir)


def test_milliways_api_unit(milliways):
    staged, app = milliways
    text = (staged / "milliways-api.service").read_text()
    assert text.startswith("# roost-managed\n")
    assert f"ExecStart={app}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8002\n" in text
    assert "/./" not in text
    assert f"WorkingDirectory={app}/backend\n" in text
    assert "User=nickg\n" in text


def test_milliways_frontend_unit(milliways):
    staged, app = milliways
    text = (staged / "milliways-frontend.service").read_text()
    assert "Environment=PORT=3002\n" in text
    assert "Environment=INTERNAL_API_URL=http://127.0.0.1:8002\n" in text
    assert f"WorkingDirectory={app}/frontend/build\n" in text


def test_milliways_nginx_tls(milliways):
    conf = (milliways[0] / "milliways.nginx.conf").read_text()
    assert "listen 443 ssl http2;" in conf
    assert "server_name milliways.nickgerrard.dev;" in conf
    assert "/etc/letsencrypt/live/milliways.nickgerrard.dev/fullchain.pem" in conf
    assert re.search(r"location /api/ \{\s+proxy_pass http://127\.0\.0\.1:8002/;", conf)
    assert re.search(r"location / \{\s+proxy_pass http://127\.0\.0\.1:3002;", conf)
    assert "return 301 https://$host$request_uri;" in conf


def test_pwhl_nginx_tls(pwhl):
    staged, app = pwhl
    conf = (staged / "pwhl.nginx.conf").read_text()
    assert "root /var/www/pwhl;" in conf
    assert re.search(r"try_files [^;]*/404\.html;", conf)
    assert re.search(r"location /api/ \{\s+proxy_pass http://127\.0\.0\.1:8000/;", conf)
    assert (staged / "www" / "index.html").read_text() == "hi"
    assert not (staged / "pwhl-frontend.service").exists()
    unit = (staged / "pwhl-api.service").read_text()
    assert f"ExecStart={app}/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000\n" in unit


@pytest.mark.parametrize("fx,name", [("milliways", "milliways"), ("pwhl", "pwhl")])
def test_http_only_variant(request, fx, name):
    staged, _ = request.getfixturevalue(fx)
    conf = (staged / f"{name}.nginx.http.conf").read_text()
    assert "listen 80;" in conf
    for bad in ("ssl", "443", "letsencrypt", "return 301"):
        assert bad not in conf
    assert conf.startswith("# roost-managed\n")


@pytest.mark.parametrize("fx,name", [("milliways", "milliways"), ("pwhl", "pwhl")])
def test_nginx_tidy(request, fx, name):
    staged, _ = request.getfixturevalue(fx)
    for f in (f"{name}.nginx.conf", f"{name}.nginx.http.conf"):
        conf = (staged / f).read_text()
        assert "\n\n\n" not in conf
        assert not re.search(r"\n\s*\n\}", conf), "blank line before closing brace"
        assert conf.endswith("}\n")
        assert conf.count("{") == conf.count("}")
