"""A managed-runtime model must report the window its child was LAUNCHED with.

Router mode cannot answer a context probe for a not-yet-loaded child: ``/props?model=X`` errors
(``proxy error: Could not establish connection``) while the bare ``/props`` documents the ROUTER
(``n_ctx: 0``). So the resolver fell through to the hardcoded catalog family match — the live
failure was a 32,768-token qwen child resolved as 131,072, after which the ~56K-token compression
prompt 400'd against the real window and compaction degraded to the deterministic fallback. For
Mason's OWN server the window is a record (``presets.ini``), not a probe.
"""

from __future__ import annotations

import importlib
import json
import os

import pytest

BASE_URL = "http://127.0.0.1:18434/v1"
MODEL = "qwen2-0_5b-instruct-q4_k_m"


@pytest.fixture
def managed_runtimes(tmp_path, monkeypatch):
    """A temp MASON_HOME whose machine-scoped runtimes dir stands in for the managed server."""
    root = tmp_path / ".mason"
    shared = root / "runtimes" / "llamacpp"
    shared.mkdir(parents=True)
    monkeypatch.setenv("MASON_HOME", str(root))
    import mason_constants

    importlib.reload(mason_constants)
    yield shared
    importlib.reload(mason_constants)


def _write_state(shared, *, base_url=BASE_URL, pid=None):
    shared.joinpath("server.json").write_text(
        json.dumps({"base_url": base_url, "api_key": "local-key", "pid": pid}), encoding="utf-8")


@pytest.mark.parametrize("window", [32768, 65536, 8192])
def test_resolution_equals_the_window_the_runtime_launched_the_child_with(managed_runtimes, window):
    """The launch record wins — with the model cold and NOTHING listening on the port, and for
    sub-64K windows too (honest reporting feeds the minimum-context gate instead of hiding it)."""
    from agent.model_metadata import get_model_context_length

    _write_state(managed_runtimes, pid=os.getpid())
    managed_runtimes.joinpath("presets.ini").write_text(f"[{MODEL}]\nctx-size = {window}\n", encoding="utf-8")

    resolved = get_model_context_length(MODEL, base_url=BASE_URL, api_key="local-key", provider="llamacpp")

    assert resolved == window, f"resolved {resolved:,} for a runtime launched at {window:,}"


def test_a_server_that_is_not_ours_never_inherits_our_preset_window(managed_runtimes):
    """Presets describe OUR supervisor's children: a leftover state file whose server is gone, or
    a foreign endpoint on one of our ports, must fall through to the live probe path."""
    from mason_cli.local_runtime.endpoint import managed_context_length

    managed_runtimes.joinpath("presets.ini").write_text(f"[{MODEL}]\nctx-size = 32768\n", encoding="utf-8")
    # 1. state file written by a supervisor that has since died
    _write_state(managed_runtimes, pid=999_999_99)
    assert managed_context_length(MODEL, BASE_URL) is None
    # 2. live supervisor, but the caller asked about a DIFFERENT endpoint
    _write_state(managed_runtimes, pid=os.getpid())
    assert managed_context_length(MODEL, "http://127.0.0.1:8080/v1") is None
    assert managed_context_length(MODEL, BASE_URL) == 32768
