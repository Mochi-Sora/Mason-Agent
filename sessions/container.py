"""Isolated session containers — each session is a folder
sessions/<id>/state.md + backup.md + backup.db (Gbrain-inspired index) + meta.json
Stored under MASON_HOME/sessions (migrated from repo tree).
"""
import pathlib, json, sqlite3, datetime, hashlib, shutil

BACKUP_LIMIT = 10_000_000  # 10M chars
BACKUP_WATCH = 0.85        # 85% triggers summary

_LEGACY_ROOT = pathlib.Path(__file__).parent.parent / "sessions"

def get_sessions_root() -> pathlib.Path:
    """MASON_HOME/sessions, with one-time migration from legacy repo location."""
    try:
        from mason_constants import get_mason_home
        p = get_mason_home() / "sessions"
    except Exception:
        p = pathlib.Path.home() / ".mason" / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    # one-time migration: move existing session dirs from legacy repo tree
    if _LEGACY_ROOT.exists() and _LEGACY_ROOT.resolve() != p.resolve():
        for child in list(_LEGACY_ROOT.iterdir()):
            if child.name.startswith("__") or child.name == "__pycache__" or child.suffix == ".py":
                continue
            dest = p / child.name
            if dest.exists():
                continue
            try:
                shutil.move(str(child), str(dest))
            except Exception:
                pass
    return p

# Back-compat alias — some callers `from sessions.container import ROOT`
# Keep it as the new location so old code keeps working.
try:
    ROOT = get_sessions_root()
except Exception:
    ROOT = _LEGACY_ROOT

def _session_dir(session_id: str) -> pathlib.Path:
    d = get_sessions_root() / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def init_session(session_id: str, user_id: str = "default") -> pathlib.Path:
    d = _session_dir(session_id)
    (d / "state.md").write_text(f"# State — {session_id}\n- Session started {datetime.datetime.utcnow().isoformat()}\n")
    (d / "backup.md").write_text(f"# Backup — {session_id}\n")
    (d / "meta.json").write_text(json.dumps({"session_id": session_id, "user_id": user_id, "started": datetime.datetime.utcnow().isoformat()}, indent=2))
    # init FTS index
    _db(d).close()
    return d

def _db(session_dir: pathlib.Path) -> sqlite3.Connection:
    db = sqlite3.connect(session_dir / "backup.db")
    db.execute("CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, text TEXT, ts TEXT)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(id, text)")
    return db

def append_backup(session_id: str, role: str, text: str):
    d = _session_dir(session_id)
    md = d / "backup.md"
    line = f"\n## {role} {datetime.datetime.utcnow().isoformat()}\n{text}\n"
    # append mode, not read+write whole file (was O(n²))
    with open(md, "a", encoding="utf-8") as f:
        f.write(line)
        try:
            f.flush()
            import os
            os.fsync(f.fileno())
        except Exception:
            pass
    # index chunk
    cid = hashlib.sha256(line.encode()).hexdigest()[:10]
    db = _db(d)
    db.execute("INSERT OR REPLACE INTO chunks(id,text,ts) VALUES(?,?,?)", (cid, line, datetime.datetime.utcnow().isoformat()))
    db.execute("INSERT OR REPLACE INTO chunks_fts(id,text) VALUES(?,?)", (cid, line))
    db.commit(); db.close()
    # watcher: >85% triggers summary via 1B (use stat, not read)
    try:
        if md.stat().st_size > BACKUP_LIMIT * BACKUP_WATCH:
            _trigger_summary(session_id)
    except Exception:
        pass

def update_state(session_id: str, content: str):
    d = _session_dir(session_id)
    (d / "state.md").write_text(content[:2000])  # cap matches injection (system_prompt caps 2000)

def append_state_bullet(session_id: str, bullet: str):
    """Auto-distill: append `tool → outcome` bullet to state.md, capped 2000 chars, keep newest."""
    try:
        d = _session_dir(session_id)
        p_state = d / "state.md"
        existing = p_state.read_text() if p_state.exists() else f"# State — {session_id}\n"
        if not existing.strip():
            existing = f"# State — {session_id}\n"
        bullet = bullet.strip()
        if not bullet.startswith("-"):
            bullet = "- " + bullet
        # timestamp if missing
        import re as _re, datetime as _dt
        if not _re.match(r"^- \d{2}:\d{2}", bullet):
            now = _dt.datetime.utcnow().strftime("%H:%M UTC — ")
            bullet = bullet.replace("- ", f"- {now}", 1)
        if not existing.endswith("\n"):
            existing += "\n"
        existing += bullet + "\n"
        # cap 2000: keep header + NEWEST bullets (same as short_term). The old fallback
        # truncated the whole file from the FRONT (`existing[:1990] + "\n…\n"`), which
        # dropped the newest bullets and kept the oldest — and since the result was still
        # over budget, every later append re-truncated to the same bytes: state.md latched
        # after the first overflow and silently stopped accepting bullets for the rest of
        # the session. Truncate the oversized bullet's own tail instead, and never write a
        # file that is still over budget.
        if len(existing) > 2000:
            lines = existing.splitlines()
            header = lines[0] if lines and lines[0].startswith("#") else f"# State — {session_id}"
            # "…" is the old truncation marker, not a bullet; dropping it lets an
            # already-latched file heal on the next append.
            bullets = [l for l in lines[1:] if l.strip() and l.strip() != "…"]
            kept = []
            cur = len(header) + 1
            for b in reversed(bullets):
                if cur + len(b) + 1 <= 2000:
                    kept.append(b)
                    cur += len(b) + 1
                else:
                    break
            kept.reverse()
            if not kept and bullets:
                # Newest bullet alone exceeds the budget: keep its head, mark the cut.
                # -1 accounts for the trailing newline so the write stays within cap.
                room = max(2000 - (len(header) + 1) - len(" …") - 1, 0)
                kept = [bullets[-1][:room].rstrip() + " …"]
            existing = header + "\n" + "\n".join(kept) + "\n"
        p_state.write_text(existing)
    except Exception:
        pass

def recall_backup(session_id: str, query: str, budget_tokens: int = 2000, limit: int = 5) -> dict:
    """Gbrain-inspired retrieval: FTS + budget packing"""
    d = get_sessions_root() / session_id
    # fallback: also check legacy location for unmigrated sessions
    if not (d / "backup.db").exists() and (_LEGACY_ROOT / session_id / "backup.db").exists():
        d = _LEGACY_ROOT / session_id
    if not (d / "backup.db").exists():
        return {"facts": [], "results": [], "budget_used": 0, "total": 0}
    db = sqlite3.connect(d / "backup.db")
    try:
        cur = db.execute("SELECT id,text FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?", (query, limit))
        rows = cur.fetchall()
    except Exception:
        cur = db.execute("SELECT id,text FROM chunks WHERE text LIKE ? LIMIT ?", (f"%{query}%", limit))
        rows = cur.fetchall()
    db.close()
    # budget pack: char/4 approx
    results = []
    used = 0
    for cid, txt in rows:
        tokens = len(txt)//4
        if used + tokens > budget_tokens:
            break
        results.append({"id": cid, "chunk": txt[:800], "evidence": "keyword_exact"})
        used += tokens
    return {"protocol_version": 1, "facts": [], "results": results, "budget_used": used, "total": len(results)}

def _trigger_summary(session_id: str):
    # delegate to 1B compressor (fire-and-forget)
    try:
        from tiered_memory.llm.tasks.backup_compress_task import build_prompt
        from tiered_memory.llm.client import LlamaClient
        d = _session_dir(session_id)
        md = d / "backup.md"
        text = md.read_text()
        # compress oldest 30%
        chunk = text[:int(len(text)*0.3)]
        prompt = build_prompt(chunk)
        summary = LlamaClient().complete(prompt, max_tokens=400)
        # replace chunk with summary
        new_text = f"# Backup — {session_id} (compressed {datetime.datetime.utcnow().isoformat()})\n## Summary of oldest 30%\n{summary}\n\n" + text[int(len(text)*0.3):]
        md.write_text(new_text)
        # rebuild both tables coherently (old code wiped FTS and left chunks stale)
        db = _db(d)
        db.execute("DELETE FROM chunks")
        db.execute("DELETE FROM chunks_fts")
        for block in new_text.split("\n## "):
            if not block.strip() or block.lstrip().startswith("# Backup"):
                continue
            line = "\n## " + block
            if len(line.strip()) < 10:
                continue
            cid = hashlib.sha256(line.encode()).hexdigest()[:10]
            ts = datetime.datetime.utcnow().isoformat()
            db.execute("INSERT OR REPLACE INTO chunks(id,text,ts) VALUES(?,?,?)", (cid, line, ts))
            db.execute("INSERT OR REPLACE INTO chunks_fts(id,text) VALUES(?,?)", (cid, line))
        db.commit(); db.close()
    except Exception:
        pass

def purge_session(session_id: str):
    d = get_sessions_root() / session_id
    if d.exists():
        shutil.rmtree(d)
    # also clean legacy if still there
    ld = _LEGACY_ROOT / session_id
    if ld.exists() and ld.resolve() != d.resolve():
        shutil.rmtree(ld, ignore_errors=True)

def purge_empty_sessions() -> list:
    """Delete zombie session dirs where backup is just the header (≤100B) and
    FTS is empty — the 6/7 empty shells the audit found. Called on every
    on_session_close so Mac/VPS self-heals without manual rm. Returns purged ids."""
    purged = []
    import time
    now = time.time()
    for root in (get_sessions_root(), _LEGACY_ROOT):
        if not root.exists():
            continue
        for p in list(root.glob("*")):
            if not p.is_dir() or p.name.startswith("__"):
                continue
            # skip code files accidentally in sessions (shouldn't happen with new layout)
            if p.suffix == ".py":
                continue
            md = p / "backup.md"
            dbp = p / "backup.db"
            try:
                # don't delete a just-started active session (header-only but fresh)
                try:
                    if now - p.stat().st_mtime < 3600:
                        continue
                except Exception:
                    pass
                if md.exists() and md.stat().st_size > 120:
                    continue
                if dbp.exists():
                    db = sqlite3.connect(dbp)
                    try:
                        cnt = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
                    except Exception:
                        cnt = 1
                    db.close()
                    if cnt != 0:
                        continue
                # empty shell → purge
                shutil.rmtree(p, ignore_errors=True)
                purged.append(p.name)
            except Exception:
                continue
    return purged

def list_sessions():
    # union of both locations for transition period
    seen = set()
    for root in (get_sessions_root(), _LEGACY_ROOT):
        if not root.exists():
            continue
        for p in root.glob("*"):
            if p.is_dir() and not p.name.startswith("__") and p.suffix != ".py":
                seen.add(p.name)
    return sorted(seen)
