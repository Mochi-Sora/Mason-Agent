"""The managed local runtime as an aux provider: reachable, boot-capable, and legible when down.

``llamacpp`` is deliberately not a PROVIDER_REGISTRY entry and its key never lands in the
environment — the supervised server is the credential — so the registry path can only ever answer
"no API key was found". These tests pin the branch contract that replaces it: resolve the managed
endpoint WITH the on-demand boot rung enabled, adopt its bearer key and served model id, and name
the real condition when the server cannot be reached at all.
"""

from __future__ import annotations

import pytest

from agent import auxiliary_client as aux

SERVED = "qwen2-0_5b-instruct-q4_k_m"


def _endpoint(**over):
    endpoint = {"base_url": "http://127.0.0.1:18434/v1", "api_key": "runtime-key"}
    endpoint.update(over)
    return endpoint


@pytest.mark.parametrize("provider", ["llamacpp", "llama.cpp", "llama-cpp"])
def test_llamacpp_alias_reaches_the_managed_runtime(monkeypatch, provider):
    monkeypatch.setattr("mason_cli.local_runtime.endpoint.resolve_llamacpp_endpoint",
                        lambda *a, **kw: _endpoint())
    monkeypatch.setattr(aux, "_llamacpp_served_model", lambda base_url, api_key: SERVED)

    client, model = aux.resolve_provider_client(provider)

    assert client is not None, "the managed runtime must be adopted, not the registry dead end"
    assert str(client.base_url).startswith("http://127.0.0.1:18434")
    assert client.api_key == "runtime-key"
    assert model == SERVED


def test_llamacpp_explicit_model_wins_over_the_served_id(monkeypatch):
    monkeypatch.setattr("mason_cli.local_runtime.endpoint.resolve_llamacpp_endpoint",
                        lambda *a, **kw: _endpoint())
    monkeypatch.setattr(aux, "_llamacpp_served_model",
                        lambda base_url, api_key: pytest.fail("served-id probe must not run"))

    client, model = aux.resolve_provider_client("llamacpp", model=SERVED)

    assert client is not None
    assert model == SERVED


def test_llamacpp_resolution_keeps_the_on_demand_boot_rung(monkeypatch):
    """A messaging gateway never boots the managed runtime, so this caller must not pass
    ``wait_for_boot_s=0``: that disables the boot, the endpoint comes back None whenever the server
    is cold, and the aux task dead-ends on the registry's "no API key" error."""
    seen: dict = {}

    def _resolve(*args, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr("mason_cli.local_runtime.endpoint.resolve_llamacpp_endpoint", _resolve)

    assert aux.resolve_provider_client("llamacpp") == (None, None)
    assert seen.get("wait_for_boot_s", 8.0) > 0, "boot rung disabled — a cold server is unreachable"


def test_unavailable_local_runtime_is_reported_as_a_server_not_a_missing_key():
    message = aux._unavailable_provider_message("llamacpp")

    assert "llama-server" in message
    assert "API_KEY" not in message

    # Non-local providers keep the credential-shaped message: their key really is missing.
    assert "API key was found" in aux._unavailable_provider_message("deepinfra")
