"""Exercise real Flask routes without fetching or loading the local GGUF model."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from werkzeug.exceptions import BadRequest


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    # backend/app.py eagerly initializes inference. Replace only that external
    # boundary; the Flask routes and authentication code remain real.
    model_file = tmp_path / "placeholder.gguf"
    model_file.touch()
    monkeypatch.setenv("MODEL_PATH", str(model_file))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")

    llama_stub = types.ModuleType("llama_cpp")
    llama_stub.Llama = lambda **kwargs: object()
    hub_stub = types.ModuleType("huggingface_hub")
    hub_stub.hf_hub_download = lambda **kwargs: str(model_file)
    monkeypatch.setitem(sys.modules, "llama_cpp", llama_stub)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub_stub)

    # Local Python installations may not have joserfc. These tests exercise
    # Flask route gating, not cryptographic verification; CI installs the real
    # package via requirements-ci.txt.
    if importlib.util.find_spec("joserfc") is None:
        jose_stub = types.ModuleType("joserfc")
        jose_stub.jwt = types.ModuleType("joserfc.jwt")
        jose_stub.jws = types.ModuleType("joserfc.jws")
        jose_stub.jws.extract_compact = lambda token: None
        jwk_stub = types.ModuleType("joserfc.jwk")
        jwk_stub.RSAKey = type("RSAKey", (), {})
        jose_stub.jwt.JWTClaimsRegistry = type("JWTClaimsRegistry", (), {})
        monkeypatch.setitem(sys.modules, "joserfc", jose_stub)
        monkeypatch.setitem(sys.modules, "joserfc.jwk", jwk_stub)
        monkeypatch.setitem(sys.modules, "joserfc.jwt", jose_stub.jwt)

    app_path = Path(__file__).resolve().parents[1] / "backend" / "app.py"
    spec = importlib.util.spec_from_file_location("coding_assistant_test_app", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    module.conn.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/protected"),
        ("get", "/history/example"),
        ("get", "/projects"),
        ("get", "/settings"),
        ("post", "/add_project"),
        ("post", "/chat"),
        ("post", "/stream"),
        ("post", "/search_web"),
        ("post", "/upload_file/example"),
        ("post", "/delete_project"),
        ("post", "/cancel"),
        ("post", "/run/example"),
        ("post", "/lint/example"),
    ],
)
def test_project_and_private_routes_require_a_bearer_token(app_module, method, path):
    response = getattr(app_module.app.test_client(), method)(path)
    assert response.status_code == 401
    assert response.get_json()["error"] == "unsupported_token_type"


def test_unknown_google_signing_key_is_rejected(app_module, monkeypatch):
    class JwksResponse:
        def json(self):
            return {"keys": []}

    monkeypatch.setattr(app_module.requests, "get", lambda *args, **kwargs: JwksResponse())
    monkeypatch.setattr(
        app_module.jws,
        "extract_compact",
        lambda token: types.SimpleNamespace(protected={"kid": "missing"}),
    )

    response = app_module.app.test_client().get(
        "/protected", headers={"Authorization": "Bearer fake-token"}
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "invalid_key"


@pytest.mark.parametrize("project", [".", "..", "../outside", "/tmp/outside"])
def test_project_path_cannot_name_or_escape_the_project_root(app_module, project):
    with app_module.app.test_request_context():
        with pytest.raises(BadRequest):
            app_module.resolve_project_path(project)
