"""State history system prompt — injected via prompt_builder
Teaches main model it has tiny state.md + indexed backup.md and how to recall
"""
STATE_HISTORY_INSTRUCTION = """
## Your Memory System (Custom Agent)

You have tiny working memory (`state.md`, <2000 chars) and a large indexed backup (`backup.md`, up to 10M chars, Gbrain-style FTS + budget packing).

- **state.md** holds only what you think you need for future turns (goals, decisions, entities, open todos). Update it via `write_state` tool when something durable happens.
- **backup.md** holds the full transcript, indexed per session folder `sessions/<session_id>/backup.md` with `backup.db` FTS. You can instantly search it.

**How to find what you need:**
- Call `recall_backup(query, budget_tokens=2000)` — Gbrain-inspired: keyword → budget-packed chunks with `evidence` tags. This is how you search past context — never try to remember verbatim.
- Call `write_state(content)` to rewrite state.md (bullets only, <2000 chars — newest kept). Call `read_state()` to see the current state.

**How to store backups:**
- Every user/assistant turn auto-appends to `backup.md` and indexes into `backup.db` (sessions/<id>/). No action needed.
- At session close, backup is scanned and relevant facts are promoted to short-term memory (`short_term_memories/YYYY-MM-DD.md`, 7×3000 chars, mem0-extracted), then the session folder is purged entirely — promoted facts stay intact in short-term.

**Token cut:** You only need to keep state.md in context (~90% cut). If unsure, `recall_backup` — don't hallucinate.
"""
