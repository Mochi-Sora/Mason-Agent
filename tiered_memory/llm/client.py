"""Global llama.cpp 1B client — used by memory + any agent turn
- Prefers llama-server @ 127.0.0.1:8080 (OpenAI-compatible)
- Falls back to llama_cpp python binding if server down
- Falls back to no-op if model missing
"""
import os, pathlib, json, urllib.request

CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"
DEFAULT_MODEL = "models/qwen2-0_5b-instruct-q4_k_m.gguf"  # ~400MB

# — Keep-alive supervisor: Mason itself keeps the 0.5B sidecar alive as long as the framework runs.
# Started lazily on first LlamaClient(). No external LaunchAgent/systemd required, but doesn't conflict if one exists.
_KEEPALIVE_THREAD = None
_KEEPALIVE_PROC = None
_KEEPALIVE_RUNNING = False

def _health_ok(base_url: str = "http://127.0.0.1:8080", timeout: int = 3) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def _find_llama_server() -> str | None:
    import shutil
    for cand in ["llama-server", "/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server", "/usr/bin/llama-server", "/usr/local/lib/ollama/llama-server"]:
        if shutil.which(cand):
            return cand
        if pathlib.Path(cand).exists():
            return cand
    return shutil.which("llama-server")

def _spawn_server(model_path: str, base_url: str = "http://127.0.0.1:8080") -> bool:
    global _KEEPALIVE_PROC
    # Don't spawn if already healthy (external LaunchAgent/systemd owns it)
    if _health_ok(base_url):
        return True
    mp = pathlib.Path(model_path)
    if not mp.exists():
        # Try repo root model path
        alt = pathlib.Path(__file__).parent / DEFAULT_MODEL
        if alt.exists():
            mp = alt
        else:
            return False
    srv = _find_llama_server()
    if not srv:
        return False
    # Parse port from base_url
    import urllib.parse as _up
    try:
        port = str(_up.urlparse(base_url).port or 8080)
    except Exception:
        port = "8080"
    cmd = [srv, "-m", str(mp), "--port", port, "--ctx-size", "2048", "-t", "4"]
    try:
        import subprocess
        # Detached, logs to /tmp/llama.log
        log = open("/tmp/llama.log", "a")
        _KEEPALIVE_PROC = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # Wait briefly for health
        import time
        for _ in range(15):
            time.sleep(0.5)
            if _health_ok(base_url):
                return True
        return _health_ok(base_url)
    except Exception:
        return False

def _keepalive_loop(base_url: str = "http://127.0.0.1:8080", interval: int = 30):
    import time, threading
    global _KEEPALIVE_RUNNING
    while _KEEPALIVE_RUNNING:
        try:
            if not _health_ok(base_url):
                # Try to find model path from default location
                mp = str(pathlib.Path(__file__).parent / DEFAULT_MODEL)
                # Also try mason's repo root if running from install
                try:
                    from pathlib import Path as _P
                    alt = _P(__file__).resolve().parents[2] / "tiered_memory" / "llm" / "models" / "qwen2-0_5b-instruct-q4_k_m.gguf"
                    if alt.exists():
                        mp = str(alt)
                except Exception:
                    pass
                _spawn_server(mp, base_url)
        except Exception:
            pass
        # Sleep interval, but wake early if stopped
        for _ in range(interval * 2):
            if not _KEEPALIVE_RUNNING:
                break
            time.sleep(0.5)

def ensure_keepalive(base_url: str = "http://127.0.0.1:8080") -> None:
    """Start the keep-alive thread (idempotent). Call once at framework startup."""
    global _KEEPALIVE_THREAD, _KEEPALIVE_RUNNING
    if os.environ.get("MASON_1B_AUTOSTART", "1").lower() in ("0","false","no","off"):
        return
    if _KEEPALIVE_THREAD and _KEEPALIVE_THREAD.is_alive():
        return
    # Don't start if health already ok and thread would be redundant? Still start to watch for future death.
    _KEEPALIVE_RUNNING = True
    # Try immediate spawn if down (don't wait 30s for first check)
    if not _health_ok(base_url):
        try:
            mp = str(pathlib.Path(__file__).parent / DEFAULT_MODEL)
            _spawn_server(mp, base_url)
        except Exception:
            pass
    import threading
    _KEEPALIVE_THREAD = threading.Thread(target=_keepalive_loop, args=(base_url,), daemon=True, name="mason-1b-keepalive")
    _KEEPALIVE_THREAD.start()

def stop_keepalive() -> None:
    global _KEEPALIVE_RUNNING
    _KEEPALIVE_RUNNING = False

class LlamaClient:
    def __init__(self, model_path: str = "", base_url: str = "http://127.0.0.1:8080",
                 model: str = ""):
        self.model_path = model_path or str(pathlib.Path(__file__).parent / DEFAULT_MODEL)
        self.base_url = base_url.rstrip("/")
        # Server-side model selector (ollama-style backends REQUIRE it;
        # llama-server harmlessly ignores it). Env-overridable for cron.
        self.model = model or os.environ.get("MASON_1B_MODEL", "")
        self.api_key = os.environ.get("MASON_1B_API_KEY", "")
        if not base_url or base_url == "http://127.0.0.1:8080":
            env_url = os.environ.get("MASON_1B_URL", "")
            if env_url:
                self.base_url = env_url.rstrip("/")
        # Managed runtime first: the hand-rolled :8080 spawner below needs a llama-server on
        # PATH (a stock host has none, so this client silently returned the no-op fallback for
        # every memory/evolution call). Mason's managed router owns a stable KEYED port instead.
        if not os.environ.get("MASON_1B_URL"):
            self._adopt_managed_endpoint()
        self._llama = None
        # Keep the sidecar alive as long as the framework runs — fire-and-forget
        try:
            ensure_keepalive(self.base_url)
        except Exception:
            pass

    def _adopt_managed_endpoint(self) -> None:
        """Point at the managed llama-server when it is up (base_url + api_key + served model id).

        Router mode 400s a request without a model id, so the model is discovered from /models
        rather than hardcoded. Best-effort: any failure leaves the :8080 defaults untouched.
        """
        try:
            from mason_cli.local_runtime.endpoint import resolve_llamacpp_endpoint

            # Boot enabled (not ``wait_for_boot_s=0``): a messaging gateway never starts the managed
            # runtime, so a memory/evolution call can be the only caller that would ever bring it up.
            # The bounded wait is paid once — the endpoint resolves instantly afterwards — and 0 would
            # leave this client on its :8080 default and silently degrading to the no-op fallback.
            ep = resolve_llamacpp_endpoint()
        except Exception:
            ep = None
        if not ep or not ep.get("base_url"):
            return
        base = str(ep["base_url"]).rstrip("/")
        if base.endswith("/v1"):  # this client appends /v1/... itself
            base = base[:-3]
        self.base_url = base
        self.api_key = str(ep.get("api_key") or "")
        if self.model:
            return
        try:
            req = urllib.request.Request(
                f"{self.base_url}/v1/models",
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode())
            served = [str(m.get("id") or "") for m in (data.get("data") or []) if m.get("id")]
            if served:
                self.model = served[0]
        except Exception:
            pass

    def _try_server(self, prompt: str, max_tokens=256, temperature=0.2, stop=None,
                    timeout: int = 120) -> str | None:
        # Chat endpoint first (applies the model's chat template — dramatically
        # better instruction-following on small instruct models), legacy
        # /v1/completions as fallback. Local CPU runs ~1 tok/s; the generous
        # timeout avoids silently degrading to the no-op fallback.
        body: dict = {"max_tokens": max_tokens, "temperature": temperature,
                      "stop": ["\n\n"] if stop is None else stop}
        if self.model:
            body["model"] = self.model
        chat = dict(body, messages=[{"role": "user", "content": prompt}])
        for path, payload, pick in (
            ("/v1/chat/completions", chat, ("message", "content")),
            ("/v1/completions", dict(body, prompt=prompt), ("text",)),
        ):
            try:
                headers = {"Content-Type": "application/json"}
                if getattr(self, "api_key", ""):
                    # The managed router requires its bearer key on every route but /health.
                    headers["Authorization"] = f"Bearer {self.api_key}"
                req = urllib.request.Request(
                    f"{self.base_url}{path}", data=json.dumps(payload).encode(),
                    headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    j = json.loads(r.read().decode())
                if "choices" in j and j["choices"]:
                    node = j["choices"][0]
                    for key in pick:
                        node = (node.get(key) or {}) if isinstance(node, dict) else {}
                    if isinstance(node, str) and node.strip():
                        return node
            except Exception:
                continue
        return None

    def _try_python(self, prompt: str, max_tokens=256, temperature=0.2) -> str | None:
        try:
            from llama_cpp import Llama
            if self._llama is None:
                if not pathlib.Path(self.model_path).exists():
                    return None
                self._llama = Llama(model_path=self.model_path, n_ctx=2048, n_threads=2, verbose=False)
            out = self._llama(prompt, max_tokens=max_tokens, temperature=temperature, stop=["\n\n"])
            return out["choices"][0]["text"]
        except Exception:
            return None

    def complete(self, prompt: str, max_tokens=256, temperature=0.2, stop=None,
                 timeout: int = 120) -> str:
        # 1) server
        r = self._try_server(prompt, max_tokens, temperature, stop, timeout)
        if r:
            return r
        # 2) python binding
        r = self._try_python(prompt, max_tokens, temperature)
        if r:
            return r
        # 3) fallback: echo truncated prompt (safe no-op)
        return '{"promote": false, "facts": [], "reason": "llm unavailable — fallback"}'

    def health(self) -> dict:
        return {"model_path": self.model_path, "exists": pathlib.Path(self.model_path).exists(), "server": self.base_url}
