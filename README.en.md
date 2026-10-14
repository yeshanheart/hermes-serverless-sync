# hermes-serverless-sync (English summary)

Two-way sync of a **Hermes Agent** install between machines through a **private Git repo** —
no server, no database, no admin rights. Syncs config / skills / memories, encrypts
`.env` + `auth.json`, and — the part most projects skip — **additively merges the session
store (`state.db`)**, so both machines keep their own new sessions instead of overwriting
each other.

**How the session merge works.** Conversation history lives in `state.db` (SQLite/WAL), not in
`sessions/` (which only holds a legacy index). Copying a live DB can yield a corrupt image, and
overwriting loses one machine's history. Instead:

1. **snapshot** — a consistent copy via SQLite's online-backup API, published as
   `snapshots/<label>.db` on a dedicated **single-commit branch** that is force-pushed, so the
   repo never grows;
2. **merge** — additive only: sessions are matched by their globally-unique TEXT primary key and
   only missing ones are inserted; their messages are re-inserted **without** `id` so
   `AUTOINCREMENT` mints fresh ids and Hermes' own triggers keep the FTS5 index, `message_uid`
   and `display_order` consistent (no manual index rebuild);
3. imported sessions are **tagged with their source machine** (`[desktop] …`, `[laptop] …`) so
   the session list stays categorised;
4. it **refuses** to merge when the local schema lacks columns the snapshot has (Hermes version
   drift) instead of guessing, and always writes a `state.db.bak-<ts>` first.

Auto-run on Windows uses the per-user Startup folder (Task Scheduler registration is often denied
to non-elevated processes): a hidden loop syncs files every 30 min and sessions every 6 h, retrying
every tick until it succeeds.

Verified end-to-end: deleting 3 sessions from a copy and merging restored the exact counts,
`messages_fts` passed `integrity-check`, and `hermes sessions list` read the result.

**Comparison:** [westsource/agentctxsync](https://github.com/westsource/agentctxsync) also syncs
Hermes sessions across devices but requires a self-hosted FastAPI + PostgreSQL server plus an MCP
client per machine; `Black-Studio-ia/hermes-sync` and `TriBrigadeMars/hermes-profile-sync` are
git-based but do not merge the session DB. This project targets the serverless, git-only case.

**Limits:** Windows-only for now; file sync is last-writer-wins; the merge is coupled to Hermes'
`state.db` schema (it fails loudly rather than corrupting); merged sessions may need a Hermes
refresh to appear.

MIT. Unofficial — not affiliated with Nous Research.
