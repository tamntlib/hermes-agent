"""Manual canonical compression gates every physical attempt with its owning declarations."""
import json

import pytest

from tests.gateway.test_compression_runtime_authority import canonical_session, fallback_session, manual_compress, URL


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [False, True])
async def test_manual_compression_opposes_live_declarations(tmp_path, monkeypatch, frozen):
    selected = "paid" if frozen else "normal"
    authority, owner, ref, client = canonical_session(tmp_path, monkeypatch, main="custom:" + selected)
    live = json.loads((tmp_path / "config.yaml").read_text())
    live["providers"][selected]["service_tiers"] = [] if frozen else ["priority"]
    (tmp_path / "config.yaml").write_text(json.dumps(live))
    try:
        reply = await manual_compress(authority, owner, ref)
        assert "result" in reply and client.requests
        assert all(request.get("extra_body", {}).get("service_tier") == ("priority" if frozen else None)
                   for request in client.requests)
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination,expected", [("paid", "priority"), ("normal", None)])
async def test_compression_fallback_uses_frozen_destination_declaration(tmp_path, monkeypatch, destination, expected):
    authority, owner, ref, first, second = fallback_session(tmp_path, monkeypatch, "custom:" + destination, URL)
    live = json.loads((tmp_path / "config.yaml").read_text())
    live["providers"]["paid"]["service_tiers"] = []
    live["providers"]["normal"]["service_tiers"] = ["priority"]
    (tmp_path / "config.yaml").write_text(json.dumps(live))
    try:
        reply = await manual_compress(authority, owner, ref)
        assert "result" in reply and first.requests and second.requests
        assert all(request.get("extra_body", {}).get("service_tier") == "priority" for request in first.requests)
        assert all(request.get("extra_body", {}).get("service_tier") == expected for request in second.requests)
    finally:
        await owner.close()
