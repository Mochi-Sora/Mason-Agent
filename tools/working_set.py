"""Session working set for Mason's cold-start tool loading.

Cold start per turn: clarify + 3 bridge tools + name:summary list (~1.2k tok).
Everything else is deferred. Tools join the session working set when the model
describes/calls them (or the predictor/heuristics pre-add them) and are then
injected as full native schemas on every later turn — so each tool costs its
schema only after first use, once per session.

Persistence: per-profile warm file at <MASON_HOME>/working_sets.json
(MASON_HOME is already profile-scoped). A new session starts warm with the
profile's last working set — routine work costs zero extra turns.

Escape hatch: 3x tool_describe for the same tool with no tool_call means the
model is stuck reading docs — auto-inject the schema (adds to valid names, so
a direct call becomes legal) instead of letting it loop.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set

_LOCK = threading.Lock()
_SETS: Dict[str, Dict[str, Any]] = {}          # session_id -> {"tools": [...], "predicted": [...]}
_DESCRIBE_NOCALL: Dict[str, Dict[str, int]] = {}  # session_id -> {tool: count}
_LOADED_PROFILES: Set[str] = set()
_PREDICT_FIRES: Set[str] = set()  # per-turn dedupe keys

ESCAPE_HATCH_DESCRIBES = 3
MAX_PREDICTED = 3
MAX_HEURISTIC = 4
WARM_CAP = 12  # warm-loaded tools per profile; steady state stays small

# Mason cold-start tool protocol (~130 tok, byte-stable all session).
# Only clarify + bridge tools are injected; every other tool is one describe
# away via the manifest inside tool_search's description. Tools the model uses
# join the session working set and stay natively available afterwards.
PROTOCOL_BLOCK = """
## Tools (cold start)
Only `clarify`, `tool_search`, `tool_describe`, `tool_call` are injected. Every
other tool is listed by name inside `tool_search`'s description — pick from that
list, load the schema with `tool_describe` (skip `tool_search` when you see the
exact name), invoke with `tool_call`. Tools you use stay available directly for
the rest of the session. NEVER invent a tool call: undescribed names are rejected.
If `tool_describe` returns nothing useful 3 times for one tool, say so and move on.
""".strip()

# Deterministic pre-inject: (regex, [tools]). Zero inference, zero latency.
_HEURISTICS: List[tuple] = [
    (r"\.(py|js|ts|md|json|yaml|toml|sh)\b|read (the )?file|show me|open ", ["read_file", "search_files"]),
    (r"```|run |execute|command|install|test |pytest|npm |pip |git |commit", ["terminal"]),
    (r"remember|recall|memor|prefer|my name|about me", ["memory", "recall_backup"]),
    (r"https?://|website|web |search (for|the web)|latest|news", ["web_search", "web_extract"]),
    (r"image|picture|photo|screenshot|see this|look at", ["vision_analyze"]),
    (r"wake|hey mason|listen", []),
    (r"schedul|cron|nightly|remind|daily", ["cronjob_manage"]),
    (r"session|backup|earlier|before|yesterday", ["recall_backup", "session_search"]),
    # State history trio: the state-first design assumes these are reachable, but
    # nothing else pre-injects write_state (it is absent from the deferred catalog,
    # so a session that never used it cannot discover it either).
    (r"state\.md|state history|working memory|write_state|read_state", ["read_state", "write_state", "recall_backup"]),
    (r"skill", ["skills_list", "skill_view"]),
    (r"delegat|parallel|subagent|background", ["delegate_task"]),
    (r"todo|task list|plan", ["todo_list"]),
    (r"clarif|which one|do you mean", ["clarify"]),
]


def _warm_file() -> Path:
    try:
        from mason_constants import get_mason_home
        return Path(get_mason_home()) / "working_sets.json"
    except Exception:
        return Path.home() / ".mason" / "working_sets.json"


def _warm_data() -> Dict[str, List[str]]:
    try:
        raw = _warm_file().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_warm(profile_key: str, tools: List[str]) -> None:
    try:
        p = _warm_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = _warm_data()
        data[profile_key] = sorted(set(tools))[:WARM_CAP]
        p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass


def _profile_key() -> str:
    import os
    try:
        from mason_constants import get_mason_home
        return str(get_mason_home())
    except Exception:
        return os.environ.get("MASON_HOME", "default")


def _ensure_session(session_id: str) -> Dict[str, Any]:
    sid = session_id or "default"
    st = _SETS.get(sid)
    if st is not None:
        return st
    key = _profile_key()
    warm: List[str] = []
    if key not in _LOADED_PROFILES:
        _LOADED_PROFILES.add(key)
        warm = [t for t in _warm_data().get(key, []) if isinstance(t, str)]
    st = {"tools": list(warm), "predicted": []}
    _SETS[sid] = st
    return st


def get(session_id: str) -> List[str]:
    """Ordered working-set tool names for this session (warm-loaded on first touch)."""
    with _LOCK:
        return list(_ensure_session(session_id)["tools"])


def add(session_id: str, names: List[str], *, predicted: bool = False) -> List[str]:
    """Add tool names to the session set. Returns the newly added ones."""
    fresh: List[str] = []
    with _LOCK:
        st = _ensure_session(session_id or "default")
        for n in names:
            name = str(n or "").strip()
            if name and name not in st["tools"]:
                st["tools"].append(name)
                fresh.append(name)
                if predicted and name not in st["predicted"]:
                    st["predicted"].append(name)
        if fresh:
            _save_warm(_profile_key(), st["tools"])
    return fresh


def note_described(session_id: str, names: List[str]) -> List[str]:
    """A tool_describe succeeded: join working set + advance escape-hatch counters."""
    sid = session_id or "default"
    auto: List[str] = []
    with _LOCK:
        st = _ensure_session(sid)
        counts = _DESCRIBE_NOCALL.setdefault(sid, {})
        for n in names:
            name = str(n or "").strip()
            if not name:
                continue
            if name not in st["tools"]:
                st["tools"].append(name)
        for name in names:
            name = str(name or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
                if counts[name] >= ESCAPE_HATCH_DESCRIBES and name not in st.get("escape_added", []):
                    st.setdefault("escape_added", []).append(name)
                    auto.append(name)
        if names:
            _save_warm(_profile_key(), st["tools"])
    return auto


def note_called(session_id: str, name: str) -> None:
    """A tool_call dispatched: tool is proven useful, reset its hatch counter."""
    sid = session_id or "default"
    with _LOCK:
        st = _ensure_session(sid)
        name = str(name or "").strip()
        if name and name not in st["tools"]:
            st["tools"].append(name)
            _save_warm(_profile_key(), st["tools"])
        counts = _DESCRIBE_NOCALL.setdefault(sid, {})
        counts.pop(name, None)


def resolve_for_api(session_id: str, *, exclude: FrozenSet[str] = frozenset()) -> List[Dict[str, Any]]:
    """Full native schemas for the session working set (registry = truth, check_fn applied)."""
    names = [n for n in get(session_id) if n not in exclude]
    if not names:
        return []
    try:
        from tools.registry import registry
        defs = registry.get_definitions(set(names), quiet=True) or []
        return [d for d in defs if isinstance(d, dict) and d.get("function", {}).get("name") in names]
    except Exception:
        return []


def heuristic_tools(user_text: str) -> List[str]:
    """Deterministic regex pre-inject from the latest user message."""
    out: List[str] = []
    text = str(user_text or "")
    for pattern, tools in _HEURISTICS:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                for t in tools:
                    if t not in out:
                        out.append(t)
        except re.error:
            continue
        if len(out) >= MAX_HEURISTIC:
            break
    return out[:MAX_HEURISTIC]


def predict_async(session_id: str, user_text: str, turn_key: str) -> None:
    """Fire-and-forget 1B tool prediction; best-effort, never blocks the turn."""
    if not user_text or turn_key in _PREDICT_FIRES:
        return
    _PREDICT_FIRES.add(turn_key)
    if len(_PREDICT_FIRES) > 500:
        _PREDICT_FIRES.clear()

    def _run() -> None:
        try:
            from tiered_memory.llm.tasks.tool_predict_task import build_prompt
            from tiered_memory.llm.client import LlamaClient
            prompt = build_prompt(user_text[:600])
            out = LlamaClient().complete(prompt, max_tokens=60)
            text = str(out or "")
            names = [n.strip().strip('"').strip("'") for n in re.split(r"[,\s]+", text) if n.strip()]
            valid = _known_tool_names()
            add(session_id, [n for n in names if n in valid][:MAX_PREDICTED], predicted=True)
        except Exception:
            pass

    th = threading.Thread(target=_run, daemon=True, name=f"ws-predict-{session_id}")
    th.start()


def _known_tool_names() -> Set[str]:
    try:
        from toolsets import _MASON_CORE_TOOLS
        return set(_MASON_CORE_TOOLS)
    except Exception:
        return set()


def reset_session(session_id: str) -> None:
    with _LOCK:
        _SETS.pop(session_id or "default", None)
        _DESCRIBE_NOCALL.pop(session_id or "default", None)


def refresh_tool_summaries(llm_1b: Any = None, limit: int = 5) -> Dict[str, str]:
    """Nightly 1B pass: sharpen up to `limit` weakest summary lines.

    Targets placeholder/seed lines first ("Mason tool — …"), then the longest
    ones. Writes tools/index/summaries.json + clears the listing cache.
    Returns {name: new_summary}. No-op without llm_1b.
    """
    import json as _json
    from pathlib import Path as _P
    idx = _P(__file__).resolve().parent / "index" / "summaries.json"
    try:
        current = _json.loads(idx.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(current, dict) or llm_1b is None:
        return {}
    stale = sorted(
        [n for n, s in current.items()
         if not isinstance(s, str) or s.startswith("Mason tool —") or len(str(s)) > 90])
    done: Dict[str, str] = {}
    # Never copy the worked example out of the task prompt (weak models echo it
    # when the description is empty — which is why empty descriptions are skipped).
    _EXAMPLE_ECHO = "search this session's indexed backup file"
    for name in stale[:max(0, limit)]:
        try:
            from tools.registry import registry as _reg
            defs = _reg.get_definitions({name}, quiet=True) or []
            desc = ""
            if defs:
                fn = (defs[0].get("function") or {}) if isinstance(defs[0], dict) else {}
                desc = str(fn.get("description", "") or "").strip()
            if not desc:
                continue  # nothing to summarize from — leave the placeholder
            from tiered_memory.llm.tasks.tool_summary_task import build_prompt
            sibs = ", ".join(
                f"{n} ({str(current.get(n, ''))[:40]})"
                for n in list(current)[:8] if n != name)
            line = llm_1b.complete(build_prompt(name, desc, sibs), max_tokens=60)
            line = str(line or "").strip().strip('"').strip("'")
            if ":" in line:
                _nm, _sum = line.split(":", 1)
                _sum = _sum.strip().rstrip(".")
                if (_nm.strip() == name and 3 < len(_sum) <= 120
                        and _sum.lower() != _EXAMPLE_ECHO.lower()):
                    current[name] = _sum
                    done[name] = current[name]
        except Exception:
            continue
    if done:
        try:
            idx.write_text(_json.dumps(dict(sorted(current.items())), indent=1,
                                       ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass
        try:
            from tools.tool_search_catalog import refresh_summaries_cache
            refresh_summaries_cache()
        except Exception:
            pass
    return done
