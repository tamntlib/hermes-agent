"""A saved Responses image reopens through the shared gateway's real media route."""

import base64
import io
import secrets

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.image_gen_provider import save_native_b64_image
from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.web_routers import files
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.dashboard_auth.session_token import SessionTokenProvider
from tests.hermes_cli.native_gateway_client import native_desktop_client

ROUTE = "/api/media"


@pytest.fixture
def saved_images(tmp_path, monkeypatch):
    launch = tmp_path / "gateway home"
    monkeypatch.setenv("HERMES_HOME", str(launch))
    images = {}
    for name, color in (("default", "black"), ("research", "red"), ("writing", "blue")):
        home = launch if name == "default" else launch / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        with io.BytesIO() as buffer:
            Image.new("RGB", (2, 3), color).save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        scope = set_hermes_home_override(home)
        try:
            images[name] = save_native_b64_image(encoded)
        finally:
            reset_hermes_home_override(scope)
    return images


@pytest.fixture
def shared_client(monkeypatch, saved_images):
    token = secrets.token_urlsafe(32)
    clear_providers()
    register_provider(SessionTokenProvider(token=token))
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "gateway.example", raising=False)
    try:
        with TestClient(web_server.app, base_url="https://gateway.example") as client:
            client.headers[web_server._SESSION_HEADER_NAME] = token
            yield client
    finally:
        clear_providers()


def _assert_saved_png(response, path):
    assert response.status_code == 200, response.text
    mediatype, encoded = response.json()["data_url"].split(",", 1)
    assert mediatype == "data:image/png;base64"
    assert base64.b64decode(encoded, validate=True) == path.read_bytes()


def test_shared_gateway_reopens_the_selected_profiles_saved_image(shared_client, saved_images):
    path = saved_images["research"]
    _assert_saved_png(shared_client.get(ROUTE, params={"path": str(path), "profile": "research"}), path)


def test_shared_gateway_does_not_fall_back_to_another_profile(shared_client, saved_images):
    path = saved_images["default"]
    _assert_saved_png(shared_client.get(ROUTE, params={"path": str(path)}), path)
    _assert_saved_png(shared_client.get(ROUTE, params={"path": str(path), "profile": "current"}), path)
    _assert_saved_png(shared_client.get(ROUTE, params={"path": str(path), "profile": "default"}), path)
    for name in ("default", "writing"):
        assert shared_client.get(ROUTE, params={"path": str(saved_images[name]), "profile": "research"}).status_code == 403
    assert shared_client.get(ROUTE, params={"path": str(saved_images["research"])}).status_code == 403


def test_shared_gateway_keeps_profile_and_auth_validation(shared_client, saved_images):
    path = str(saved_images["research"])
    for profile, status in (("unknown", 404), ("invalid name", 400)):
        assert shared_client.get(ROUTE, params={"path": path, "profile": profile}).status_code == status
    with TestClient(web_server.app, base_url="https://gateway.example") as unauthenticated:
        response = unauthenticated.get(ROUTE, params={"path": path, "profile": "research"})
        assert response.status_code == 401


def test_shared_gateway_limits_named_profile_reads_to_saved_native_images(shared_client, saved_images):
    path = saved_images["research"]
    ordinary = path.with_name("ordinary.png")
    ordinary.write_bytes(path.read_bytes())
    with_named_dir = path.parent / "nested"
    with_named_dir.mkdir()
    nested = with_named_dir / path.name
    nested.write_bytes(path.read_bytes())
    unsupported = path.with_suffix(".txt")
    unsupported.write_bytes(b"fixture")
    missing = path.with_name("responses_image_20261120_000000_abcdef12.png")
    for target, status in ((ordinary, 403), (nested, 403), (unsupported, 415), (missing, 404)):
        response = shared_client.get(ROUTE, params={"path": str(target), "profile": "research"})
        assert response.status_code == status, response.text
    with path.open("r+b") as handle:
        handle.truncate(files._MEDIA_MAX_BYTES + 1)
    assert shared_client.get(ROUTE, params={"path": str(path), "profile": "research"}).status_code == 413


def test_shared_gateway_keeps_a_linked_generated_directory_outside_the_profile_denied(shared_client, saved_images):
    path = saved_images["research"]
    path.parent.rename(path.parent.with_name("original-images"))
    sibling_dir = saved_images["writing"].parent
    path.parent.symlink_to(sibling_dir, target_is_directory=True)
    response = shared_client.get(ROUTE, params={"path": str(path.parent / saved_images["writing"].name), "profile": "research"})
    assert response.status_code == 403


def test_private_grants_still_bind_their_own_home_and_refuse_other_profiles(monkeypatch, saved_images):
    path = saved_images["research"]
    with native_desktop_client(monkeypatch, {"research": path.parents[3]}) as client:
        _assert_saved_png(client.get(ROUTE, params={"path": str(path), "profile": "research"}), path)
        # The fixture sends a valid launch-home grant when the selector has no matching grant.
        mismatch = client.get(ROUTE, params={"path": str(saved_images["writing"]), "profile": "writing"})
        assert mismatch.status_code == 403
        assert mismatch.json()["detail"] == "profile_scope_mismatch"
        assert client.get(ROUTE, params={"path": str(path), "profile": "current"}).status_code == 403
