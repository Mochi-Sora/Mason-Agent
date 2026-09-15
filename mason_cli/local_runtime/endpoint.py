"""Endpoint resolution for llamacpp-alias requests (provider integration).

``provider: llamacpp`` with no explicit base_url resolves, in order, to the managed server (state
file), a detected external llama-server, or — during a backend boot race — the managed server once
its state file appears.
"""

from __future__ import annotations

from contextlib import suppress
import json
import logging
import threading
import time
import urllib.request

LLAMACPP_ALIASES = frozenset({"llamacpp", "llama.cpp", "llama-cpp"})

logger = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    """Liveness for the state file's supervisor-child pid: psutil when available, else True
    (optimistic). On Windows ``os.kill(pid, 0)`` TERMINATES the process — never use it as a probe."""
    if not pid or pid < 0:
        return False
    with suppress(Exception):
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    return True


def _state_endpoint() -> dict | None:
    from mason_cli.local_runtime.supervisor import state_path

    path = state_path()
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    base_url = state.get("base_url", "")
    if not base_url:
        return None
    # Ownership proof: on the stable port a SECOND install (different MASON_HOME) can own
    # 127.0.0.1:18434 with a different api key while this install's state file still points
    # there. /health is public and answers 200 for ANYONE's server — trusting it sent every
    # request at a server that 401s our key, silently — so the recorded supervisor pid is the
    # ONLY tiebreaker: a live pid is ours (healthy, or STARTING — state is written at spawn, and
    # readiness probes racing the boot must see a configured provider, not missing credentials);
    # a dead pid is a crashed-without-cleanup leftover, ignored so requests don't blackhole.
    if not _pid_alive(int(state.get("pid") or 0)):
        return None
    return {"base_url": base_url, "api_key": state.get("api_key", "")}


def managed_root() -> "tuple[str, str] | None":
    """(base_root, api_key) of the managed router, or None. Resolved through the
    ownership-guarded reader, not a raw state-file read: on the shared stable port a foreign
    install's server answers /health for anyone, and a raw read would attach callers to someone
    else's server."""
    with suppress(Exception):
        state = _state_endpoint()
        if state is None:
            return None
        base = str(state.get("base_url", "")).rsplit("/v1", 1)[0]
        return (base, str(state.get("api_key", ""))) if base else None
    return None


def managed_get_json(base: str, api_key: str, route: str, timeout_s: float) -> object:
    """Authenticated GET against the managed router; raises on any transport/decode failure."""
    req = urllib.request.Request(f"{base}{route}",
                                 headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.loads(r.read())


def _origin(url: str) -> str:
    """``scheme://host:port`` — path-insensitive, so a caller's ``…/v1/`` and the state file's
    ``…/v1`` compare equal while another install's server on the stable port does not."""
    from urllib.parse import urlparse

    parsed = urlparse(str(url or "").strip())
    return f"{parsed.scheme.lower()}://{parsed.hostname or ''}:{parsed.port or ''}"


def managed_context_length(model: str, base_url: str = "") -> int | None:
    """Context window the managed runtime will serve ``model``, or None.

    The runtime is OURS, so the answer is a record, not a probe: ``presets.ini`` is the launch
    decision the router was handed, and it is known BEFORE the child is loaded. Probing router
    mode instead is not merely slower, it reports the wrong server: a cold child answers
    ``/props?model=`` with an error and the bare ``/props`` is the ROUTER's own document
    (``n_ctx: 0``), so callers fall through to a catalog family guess (a 32K qwen model ->
    131,072) and then build a prompt the server cannot hold — observed as a 56K-token summary
    prompt 400ing against a 32,768-token window.

    Ownership-gated through ``_state_endpoint()`` (live supervisor pid) and origin-matched to
    ``base_url``: a foreign llama-server, or a leftover state file whose server is gone, must
    never inherit this install's preset decisions.
    """
    with suppress(Exception):
        state = _state_endpoint()
        if state is None:
            return None
        if base_url and _origin(base_url) != _origin(str(state.get("base_url", ""))):
            return None
        from mason_cli.local_runtime.presets import read_preset_decisions

        decisions = read_preset_decisions()
        entry = decisions.get(model) or decisions.get(str(model).rsplit("/", 1)[-1])
        window = int(getattr(entry, "window", 0) or 0)
        return window if window > 0 else None
    return None


def resolve_llamacpp_endpoint(config: dict | None = None,
                              wait_for_boot_s: float = 8.0) -> dict | None:
    """Managed-first, detection-second endpoint for llamacpp aliases.

    Boot-race rung: on a fresh backend start there is NO state file yet — the lifespan boot thread
    is still spawning the server (≈1-3 s) while the desktop's readiness probe fires the moment the
    WebSocket connects.
    """
    managed = _state_endpoint()
    if managed:
        return managed

    from mason_cli.local_runtime.detect import detect_server

    ports = ((config or {}).get("local_runtime") or {}).get("detect_ports") or []
    hit = detect_server(extra_ports=tuple(int(p) for p in ports))
    if hit and not hit.auth_required:
        return {"base_url": hit.base_url, "api_key": ""}

    if wait_for_boot_s > 0 and _boot_in_flight(config):
        _kick_managed_boot(config)
        deadline = time.monotonic() + wait_for_boot_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
            managed = _state_endpoint()
            if managed:
                return managed
    return None


_KICK_LOCK = threading.Lock()


def _load_config_if_none(config: dict | None) -> dict | None:
    if config is not None:
        return config
    from mason_cli.config import load_config

    return load_config()


def _kick_managed_boot(config: dict | None) -> None:
    """Actively start the managed server when resolution finds it missing — the wait loop assumes
    some OTHER thread is bringing it up, which is true only at backend start."""
    if not _KICK_LOCK.acquire(blocking=False):
        return  # a kick is already in flight

    def _boot() -> None:
        try:
            from mason_cli.local_runtime.bootstrap import ensure_local_runtime

            ensure_local_runtime(_load_config_if_none(config))
        except Exception:  # noqa: BLE001 — best-effort; resolution falls back
            logger.warning("on-demand managed-server boot failed", exc_info=True)
        finally:
            _KICK_LOCK.release()

    threading.Thread(target=_boot, daemon=True,
                     name="lr-on-demand-boot").start()


def _boot_in_flight(config: dict | None) -> bool:
    """True when a managed-runtime boot can actually succeed — enabled, installed, AND something to
    serve (a verified-manifest scan under runtimes_root(), NOT a bare ``server_binary()`` call —
    that needs an install_dir, and calling it bare once made this gate throw-and-return False
    forever, disabling the boot wait).

    The staged-model rung mirrors ``ensure_local_runtime``'s own residency rule (no staged models =>
    it returns without booting), so without it every caller that waits for a state file waits out
    the FULL timeout for a server that was never started — an 8 s stall per aux call on an install
    whose aux tasks default to the local runtime.
    """
    with suppress(Exception):
        config = _load_config_if_none(config)
        if not ((config or {}).get("local_runtime") or {}).get("enabled"):
            return False
        from mason_cli.local_runtime.binaries import manifest_verified, runtimes_root

        if not any(manifest_verified(m) for m in runtimes_root().glob("*/*/manifest.json")):
            return False
        from mason_cli.local_runtime.bootstrap import staged_models

        return bool(staged_models())
    return False
