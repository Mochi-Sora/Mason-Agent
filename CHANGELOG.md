# Changelog

All notable changes to this fork of Mason. Newest first.

## Unreleased — 2026-09-15

**Local model runtime (`llamacpp`) as a real auxiliary provider, plus four adjacent runtime bugs.**

All of it came out of one failure class: auxiliary tasks (`compression`, `approval`, `review`,
`mcp`, `title_generation`, `memory_query_rewrite`, `triage_specifier`) pointed at the local model
never reached it — and the runtime's own bookkeeping (context window, install verification,
boot-capability) misreported state in ways that produced malformed requests or silent stalls.

### mason_cli/config_defaults.py — the default really names the managed runtime
- **Was:** the local-1B default override wrote `provider: local` + `base_url: http://127.0.0.1:8080`.
- **Why it failed:** `local` is not a provider alias (`llamacpp` / `llama.cpp` / `llama-cpp` are) and
  nothing binds `:8080` — the managed server uses a stable, keyed port. Every aux task without an
  explicit config entry dead-ended on "no API key was found" (or a placeholder-key 401).
- **Now:** `provider: llamacpp`, `model: ""`, `base_url: ""`. Empty model/base_url defer to the aux
  router, which resolves the managed endpoint at call time and adopts whichever GGUF the running
  server serves.

### agent/auxiliary_client.py — an explicit branch for the managed runtime
- New `_resolve_llamacpp_branch()` + `_llamacpp_served_model()`, registered for all three aliases via
  `_LOCAL_RUNTIME_PROVIDER_ALIASES`.
- **Why:** `llamacpp` is deliberately absent from `PROVIDER_REGISTRY` and its key never lands in the
  environment — the supervised server *is* the credential — so the registry path could only ever
  answer "no API key was found". The branch mirrors the main-model path
  (`mason_cli.runtime_provider_custom._resolve_llamacpp_runtime`): resolve the supervised endpoint,
  adopt its bearer key, and discover the served model id when the caller names none.
- The on-demand boot rung stays enabled (`resolve_llamacpp_endpoint()`, `wait_for_boot_s > 0`): a
  messaging gateway never boots the managed runtime, so an aux task is often the only caller that
  would ever bring it up.
- Model pre-fill from the configured main model is excluded for these aliases: the router rejects a
  model it does not serve, and the main chat model is never a local one.
- New `_unavailable_provider_message()`: when an explicitly configured provider resolves to no
  client, the managed-runtime case now says the server isn't running (and how to start it) instead of
  the misleading "Set LLAMACPP_API_KEY".

### mason_cli/local_runtime/endpoint.py
- New `managed_context_length(model, base_url)`: the window the runtime will serve, read from the
  launch record (`presets.ini`) rather than probed. Ownership-gated (live supervisor pid) and
  origin-matched to `base_url`, so a foreign llama-server — or a leftover state file whose server is
  gone — never inherits this install's preset decisions.
- `_boot_in_flight()` now also requires staged models, mirroring `ensure_local_runtime`'s residency
  rule. Without it, every caller waited out the full timeout (8 s per aux call) for a server that was
  never going to start on an install with no GGUF staged.

### agent/model_metadata.py
- `_query_local_context_length()` consults `managed_context_length()` before any probe.
- **Why:** in router mode a not-yet-loaded child answers no probe, so a lazily-loaded 32,768-token
  qwen child resolved as 131,072 (the catalog family guess), a ~56K-token summary prompt 400'd, and
  compaction degraded to its deterministic fallback.
- Corrected the `_llamacpp_context` docstring: `?model=` needs a live child, and bare `/props`
  documents the ROUTER (`n_ctx: 0`), not the model.

### mason_cli/local_runtime/binaries.py — `verify_install()` cannot "verify" a binary that cannot run
- **Bug:** the tag check was a substring match over stdout+stderr. A prebuilt needing glibc 2.38 on a
  glibc 2.35 host prints a loader error containing the install path — and the path contains the tag —
  so it recorded `verified_version` = the loader error, was reported as an installed tag, and failed
  the managed boot with rc=1.
- **Now:** loader/dynamic-link markers (GLIBC, GLIBCXX, "not found (required by", "error while loading
  shared libraries", "cannot execute binary file") raise immediately, and the tag is matched only
  inside version lines.

### sessions/container.py — `append_state_bullet` stops latching `state.md`
- **Bug:** the over-budget fallback cut the file from the FRONT and re-applied the same cut on every
  append, so `state.md` kept the oldest bullets, dropped the newest, and — still over budget — kept
  truncating to identical bytes. After the first overflow the file silently stopped accepting bullets
  for the rest of the session.
- **Now:** keep header + newest bullets (the old "…" marker is dropped, so an already-latched file
  heals), and if the newest bullet alone is over budget keep its head with an explicit cut mark.
  Never writes a file that is still over budget.

### tiered_memory/llm/client.py
- Adopts the managed endpoint (base_url + api_key + served model id) when `MASON_1B_URL` is unset, and
  sends the bearer key on requests.
- **Why:** the hand-rolled `:8080` spawner needs a `llama-server` on PATH, which a stock host does not
  have — so memory/evolution calls silently returned the no-op fallback.

### agent/state_history_prompt.py + tools/working_set.py — the state tools are actually reachable
- The state-history prompt told the agent to call `update_state(content)`; the real tool is
  `write_state`, and it now states the budget rule (bullets only, newest kept).
- `write_state` is absent from the deferred tool catalog, so a session that never used it could not
  discover it at all. The working-set heuristics now pre-inject `read_state`, `write_state` and
  `recall_backup` for state / working-memory / earlier-session queries.

### Tests
- Updated: `tests/mason_cli/test_aux_config.py`, `tests/mason_cli/test_local_runtime.py`.
- Added: `tests/agent/test_auxiliary_llamacpp_managed_runtime.py`,
  `tests/agent/test_managed_runtime_context_window.py`.

### agent/conversation_compression.py — a settled cancelled worker is not an orphan
- **Bug:** `_join_cancelled_worker` read *any* `concurrent.futures.TimeoutError` as "the join's grace
  expired, the thread is still running". A job admitted before the ceiling but STARTED after it is
  refused pre-start by `_fence_gated_worker` — which raises that exact exception class into the
  future. On a loaded host the shared compress-timeout pool starts the job late, so the join met a
  settled future, reported a phantom orphan, and retained the durable lease for an attempt that never
  acquired it (whose release hook can therefore never fire).
- **Now:** a future that is already `done()` is a dead thread — treated as joined, with any exception
  it carries logged at debug. The `TimeoutError` branch re-checks `done()` for a settle that races in
  during the wait. Only a still-pending future is an orphan.
- **Tests:** `tests/agent/test_compression_attempt_lifecycle.py` — new
  `TestCancelledWorkerJoinClassification` (4 cases) pins the classification directly, and the two
  ceiling tests move to budgets (2.0s / 1.0s) that leave the shared pool room to actually START the
  job, with the cooperative test's unwind raised to 0.5s so removing the join still fails it.
