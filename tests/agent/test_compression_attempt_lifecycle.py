"""Attempt-lifecycle regression tests for #97488 / #96775.

Pins the three attempt-level lifecycle guarantees added after PR #98628
collapsed lean compaction to one auxiliary request per attempt:

1. A ceiling/idle-timeout host TEARS DOWN its worker: a cooperative worker is
   joined within the bounded grace (releasing the lease normally), while an
   uninterruptible worker is orphaned behind the poison fence — its late
   result is discarded and, on the total-ceiling path, the durable lease is
   retained until it exits so no new attempt overlaps the unchanged session.
2. A failed/stalled/cancelled attempt records a durable per-session backoff
   (strategy + failure kind stamped into the state.db cooldown row) that
   SURVIVES a gateway restart; the next automatic turn skips the same
   strategy inside the window, and a successful compression clears it.
3. Late results from a superseded attempt are discarded, never committed
   over newer state (generation counter), and a transiently-blocked no-op is
   reported as a soft defer — never compression_exhausted (false auto-reset).
"""

from __future__ import annotations

import concurrent.futures
import copy
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.auxiliary_client import AuxiliaryExplicitCancellation
from agent.conversation_compression import (
    CompressionCommitFence,
    _claim_compressor_attempt,
    _join_cancelled_worker,
    compress_context,
    compression_blocked_transiently,
    run_compress_context_with_progress_timeout,
)
from mason_state import SessionDB


def _build_agent(tmp_path: Path, session_id: str, db: SessionDB | None = None):
    if db is None:
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id, source="cli")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    agent._cached_system_prompt = "sys"
    agent.context_compressor.threshold_tokens = 1_000
    return db, agent


def _messages():
    return [{"role": "user", "content": f"m{i}"} for i in range(20)]


class TestWorkerTeardownOnCeiling:
    def test_cooperative_worker_joined_within_grace(self):
        """A worker that exits promptly after cancel is joined on the
        total-ceiling path; the lease is released normally (no retention) —
        the sabotage check for this test is removing the
        `_join_cancelled_worker` call, which makes
        `worker_done.is_set()` False when the host returns."""
        original = [{"role": "user", "content": "keep"}]
        worker_done = threading.Event()

        def cooperative_worker(fence: CompressionCommitFence):
            # Continuous progress (the #97488 'last progress 0.0s ago'
            # shape) so only the TOTAL ceiling expires; poll the poison
            # fence like the production worker does between provider phases.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if fence.is_cancelled:
                    break
                fence.touch_progress()
                time.sleep(0.01)
            # Cooperative-but-not-instant exit: the unwind after seeing the
            # poison takes real time (rollback, telemetry). It must be long enough that the
            # host's OWN post-ceiling path (~ms of bookkeeping, but starvable on a loaded box)
            # can never outlast it: otherwise removing `_join_cancelled_worker` still passes,
            # because the host's wait loop adopts the worker's own late return instead.
            time.sleep(0.5)
            worker_done.set()
            return (original, "late")

        fence = CompressionCommitFence()
        # Budgets leave the join room to reap the 0.5s unwind above: the join's effective grace is
        # min(_CANCELLED_WORKER_TEARDOWN_GRACE_SECONDS, ceiling) == the ceiling, so the ceiling must be
        # both (a) comfortably larger than the unwind, and (b) long enough that the SHARED
        # compress-timeout pool actually starts the job — a job that starts after the deadline is
        # refused pre-start by _fence_gated_worker, and then worker_done is unset for a reason this
        # test does not mean to assert.
        msgs, prompt = run_compress_context_with_progress_timeout(
            worker=cooperative_worker,
            messages=original,
            system_prompt_fallback="fallback",
            idle_timeout_seconds=2.0,
            total_ceiling_seconds=2.0,
            fence=fence,
            stall_fallback=False,
        )
        # The bounded-grace join must have reaped the cooperative worker
        # BEFORE the host returned.
        assert worker_done.is_set(), (
            "host returned before tearing down a cooperative cancelled "
            "worker — bounded-grace join missing (#97488)"
        )
        # Whichever return path won the race (fallback via join, or the
        # worker's own return adopted inside the final wait slice), the
        # transcript must be unchanged.
        assert msgs == [{"role": "user", "content": "keep"}]
        assert prompt in ("fallback", "late")
        # Teardown proved quiescence, so the lease must NOT stay retained.
        assert fence._retain_cancelled_lock_until_worker_done is False

    def test_uninterruptible_worker_is_orphaned_with_lease_retained(self):
        """A worker stuck in an uninterruptible provider call is orphaned:
        the host returns after the grace, the poison fence discards its late
        result, and on the total-ceiling path the durable lease release hook
        does NOT fire while the worker is alive (no overlap window)."""
        original = [{"role": "user", "content": "keep"}]
        release = threading.Event()
        worker_finished = threading.Event()
        started = threading.Event()
        lock_released: list[float] = []

        def stuck_worker(fence: CompressionCommitFence):
            # Continuous progress so only the TOTAL ceiling can expire
            # (the #97488 'last progress 0.0s ago' shape).
            started.set()
            while not release.wait(timeout=0.02):
                fence.touch_progress()
            worker_finished.set()
            if not fence.begin_commit():
                return (original, "")
            try:
                return ([{"role": "assistant", "content": "late"}], "late")
            finally:
                fence.finish_commit()

        fence = CompressionCommitFence()
        fence.register_cancelled_lock_release(
            lambda: lock_released.append(time.monotonic())
        )
        # Budgets match the cooperative test: the join's grace is
        # min(_CANCELLED_WORKER_TEARDOWN_GRACE_SECONDS, ceiling) == the ceiling, so the ceiling
        # must leave room for the shared pool to START the job (a pre-start refusal would make
        # "worker still alive" true for the wrong reason, or release the lease below).
        msgs, prompt = run_compress_context_with_progress_timeout(
            worker=stuck_worker,
            messages=original,
            system_prompt_fallback="fallback",
            idle_timeout_seconds=1.0,
            total_ceiling_seconds=1.0,
            fence=fence,
            stall_fallback=False,
        )
        # Precondition: the job actually started, and the worker is still running.
        assert started.wait(timeout=5), "compression worker never started — test did not exercise an orphan"
        assert not worker_finished.is_set()
        assert msgs is original and prompt == "fallback"
        # Total-ceiling path: lease retained until the worker exits, so no
        # new attempt can overlap the unchanged session.
        assert not lock_released, (
            "durable lease released while the timed-out worker was still "
            "alive — overlap window reopened (#97488)"
        )
        release.set()
        assert worker_finished.wait(timeout=2)
        # Late result was fence-poisoned, never adopted.
        assert msgs == [{"role": "user", "content": "keep"}]


class TestCancelledWorkerJoinClassification:
    """The teardown join must read "future already settled" as "thread gone".

    A job admitted before the deadline but STARTED after it is refused pre-start by
    ``_fence_gated_worker``, which raises ``concurrent.futures.TimeoutError`` into the future —
    the same exception class the join raises when its grace expires. A future that is already
    settled is not an orphan: reporting one logs a phantom "did not exit within grace" warning and
    retains the durable lease for an attempt that never acquired it (its release hook can never
    fire). Observed as a flaky
    ``TestWorkerTeardownOnCeiling::test_cooperative_worker_joined_within_grace`` on a loaded box.
    """

    def test_settled_pre_start_refusal_is_not_an_orphan(self):
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_exception(
            concurrent.futures.TimeoutError("compression deadline expired before worker start")
        )
        assert _join_cancelled_worker(future, 0.2) is True, (
            "a worker refused before start is a dead thread, not a live provider call"
        )

    def test_cancelled_before_start_is_not_an_orphan(self):
        future: concurrent.futures.Future = concurrent.futures.Future()
        assert future.cancel()
        assert _join_cancelled_worker(future, 0.2) is True

    def test_settled_result_is_joined(self):
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result(([{"role": "user", "content": "keep"}], "late"))
        assert _join_cancelled_worker(future, 0.2) is True

    def test_pending_worker_is_still_an_orphan(self):
        future: concurrent.futures.Future = concurrent.futures.Future()
        assert _join_cancelled_worker(future, 0.05) is False, (
            "a worker still running after the grace must be reported as an orphan"
        )


class TestDurableAttemptBackoff:
    def test_backoff_row_records_strategy_and_kind(self, tmp_path: Path):
        db, agent = _build_agent(tmp_path, "BACKOFF_KIND")
        agent.context_compressor.record_timeout_failure(
            "host ceiling exhausted", failure_kind="ceiling_exhausted"
        )
        row = db.get_compression_failure_cooldown("BACKOFF_KIND")
        assert row is not None, "backoff must persist to state.db"
        assert row["remaining_seconds"] > 0
        assert "backoff:ceiling_exhausted:strategy=lean" in (row["error"] or "")

    def test_backoff_blocks_same_strategy_reentry_next_turn(self, tmp_path: Path):
        db, agent = _build_agent(tmp_path, "BACKOFF_REENTRY")
        agent.context_compressor.record_timeout_failure(
            "stall", failure_kind="stalled"
        )
        # Precondition: over threshold, so only the backoff can block.
        assert 500_000 >= agent.context_compressor.threshold_tokens
        should, reason = agent.context_compressor.should_compress_info(500_000)
        assert should is False
        assert reason and reason.startswith("cooldown"), (
            "next-turn re-entry of the same strategy must be skipped inside "
            "the backoff window (#96775)"
        )

    def test_backoff_survives_simulated_gateway_restart(self, tmp_path: Path):
        db, agent = _build_agent(tmp_path, "BACKOFF_RESTART")
        agent.context_compressor.record_timeout_failure(
            "stall before restart", failure_kind="stall_interrupted"
        )
        # Precondition: row is durable in this DB file.
        assert db.get_compression_failure_cooldown("BACKOFF_RESTART")
        # Simulated restart: brand-new SessionDB handle + brand-new agent
        # objects rebuilt from the same state.db file.
        db2 = SessionDB(db_path=tmp_path / "state.db")
        _db2, agent2 = _build_agent(tmp_path, "BACKOFF_RESTART", db=db2)
        cooldown = agent2.context_compressor.get_active_compression_failure_cooldown(
            refresh=True
        )
        assert cooldown is not None, (
            "backoff must survive a gateway restart via state.db (#96775)"
        )
        assert "stall_interrupted" in (cooldown["error"] or "")
        should, reason = agent2.context_compressor.should_compress_info(500_000)
        assert should is False and reason.startswith("cooldown")

    def test_success_clears_backoff(self, tmp_path: Path):
        db, agent = _build_agent(tmp_path, "BACKOFF_CLEAR")
        compressor = agent.context_compressor
        compressor.record_timeout_failure("stall", failure_kind="stalled")
        assert db.get_compression_failure_cooldown("BACKOFF_CLEAR")
        # What a successful compression does on commit:
        compressor._clear_compression_failure_cooldown()
        assert db.get_compression_failure_cooldown("BACKOFF_CLEAR") is None
        assert compressor.should_compress_info(500_000)[0] is True


class TestSupersessionDiscardsLateResults:
    def test_superseded_attempt_candidate_never_commits(self, tmp_path: Path):
        db, agent = _build_agent(tmp_path, "SUPERSEDE")
        live = _messages()
        original = copy.deepcopy(live)

        def compress_and_get_superseded(messages, **_kwargs):
            # While this attempt's summary was in flight, a NEWER attempt
            # claimed the compressor (what a retry/fallback does).
            _claim_compressor_attempt(agent.context_compressor)
            return [{"role": "assistant", "content": "stale summary"}]

        agent.context_compressor.compress = compress_and_get_superseded
        out, _prompt = compress_context(
            agent, live, "sys", approx_tokens=500_000
        )
        assert out == original, (
            "late candidate from a superseded attempt must be discarded, "
            "never committed over newer state (#97488)"
        )
        assert live == original
        # Session stayed writable and unrotated.
        assert db.get_compression_lock_holder("SUPERSEDE") is None
        db.append_message("SUPERSEDE", "assistant", "still writable")


class TestTransientBlockIsNotExhaustion:
    def test_cooldown_blocked_noop_sets_transient_signal(self, tmp_path: Path):
        db, agent = _build_agent(tmp_path, "TRANSIENT_SIGNAL")
        agent.context_compressor.record_timeout_failure(
            "host ceiling", failure_kind="ceiling_exhausted"
        )
        live = _messages()
        before = copy.deepcopy(live)
        out, _ = compress_context(agent, live, "sys", approx_tokens=500_000)
        # Preconditions: the pass no-oped and it was NOT a lock skip.
        assert out == before
        assert getattr(agent, "_compression_skipped_due_to_lock", None) is None
        assert compression_blocked_transiently(agent) is True, (
            "a cooldown-blocked no-op must be distinguishable from "
            "exhaustion or the gateway falsely auto-resets (#97488)"
        )

    def test_signal_cleared_per_attempt_and_not_set_when_unblocked(
        self, tmp_path: Path
    ):
        db, agent = _build_agent(tmp_path, "TRANSIENT_CLEAR")
        # Stale signal from a previous pass must not leak.
        agent._compression_blocked_transient = "cooldown:999"
        agent.context_compressor.compress = lambda messages, **kw: list(messages)
        live = _messages()
        compress_context(agent, live, "sys", approx_tokens=500_000)
        assert compression_blocked_transiently(agent) is False

    def test_type_pinned_against_magicmock_agents(self):
        from unittest.mock import MagicMock

        mock_agent = MagicMock()
        # MagicMock auto-attributes are truthy but not str.
        assert compression_blocked_transiently(mock_agent) is False


def _summary_response(content: str):
    from unittest.mock import MagicMock

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return response


class TestProviderOverflowBypassesCooldown:
    """#100661: a provider-proven overflow must get one REAL summary attempt
    while the summary-failure cooldown is armed. Before the fix every turn of
    a wedged session hit the cooldown gate, returned the soft "temporarily
    paused" deferral, and the next failure extended the ladder — 4 long
    sessions were lost this way. Ordinary (non-overflow) automatic passes
    must still defer."""

    def _armed_agent(self, tmp_path: Path, session_id: str):
        db, agent = _build_agent(tmp_path, session_id)
        # Realistic arming: a failed/stalled attempt recorded the ladder.
        agent.context_compressor.record_timeout_failure(
            "stall", failure_kind="stalled"
        )
        assert agent.context_compressor.should_compress_info(500_000)[0] is False
        return db, agent

    def test_overflow_attempt_invokes_summarizer_while_cooldown_armed(
        self, tmp_path: Path
    ):
        db, agent = self._armed_agent(tmp_path, "OVERFLOW_BYPASS")
        calls = []

        def fake_call_llm(**kwargs):
            calls.append(kwargs)
            return _summary_response("## Goal\nRecovered after overflow.")

        # Bulky turns so the compacted transcript is genuinely smaller.
        live = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " * 400}
            for i in range(20)
        ]
        with patch("agent.context_compressor.call_llm", fake_call_llm):
            out, _ = compress_context(
                agent, live, "sys", approx_tokens=500_000, bypass_cooldown=True
            )
        assert len(calls) == 1, (
            "provider-proven overflow must reach the summary LLM even while "
            "the failure cooldown is armed (#100661)"
        )
        assert compression_blocked_transiently(agent) is False
        assert len(out) < len(live), "the attempt must actually compact"

    def test_non_overflow_pass_still_deferred_by_cooldown(self, tmp_path: Path):
        db, agent = self._armed_agent(tmp_path, "OVERFLOW_ORDINARY")
        calls = []

        def fake_call_llm(**kwargs):  # pragma: no cover - must not run
            calls.append(kwargs)
            return _summary_response("unexpected")

        live = _messages()
        before = copy.deepcopy(live)
        with patch("agent.context_compressor.call_llm", fake_call_llm):
            out, _ = compress_context(agent, live, "sys", approx_tokens=500_000)
        assert calls == [] and out == before
        assert compression_blocked_transiently(agent) is True, (
            "ordinary threshold pressure keeps honoring the cooldown (#11529)"
        )
