# hermes-serverless-sync

**Two-way sync of a [Hermes Agent](https://hermes-agent.nousresearch.com/docs) install across machines through a private Git repo — no server, no database, no admin rights.**

And it does the part most projects skip: **conversation history (`state.db`) is merged additively**, instead of overwriting one machine with the other or giving up on sessions entirely.

> ⚠️ Unofficial, not affiliated with Nous Research. Hermes has no built-in cross-device sync (see [issue #20510](https://github.com/NousResearch/hermes-agent/issues/20510)).
> 中文说明见 [README.zh-CN.md](README.zh-CN.md)。

---

## The problem

A Hermes install keeps everything locally (on Windows, `%LOCALAPPDATA%\hermes`):

- `config.yaml`, `SOUL.md`, `skills/`, `memories/`, `sessions/`
- `.env`, `auth.json` — credentials
- **`state.db`** — SQLite, holding **the entire message history of every session** (`sessions/` only carries a legacy index)

Each machine writes its own copy, so switching computers means amnesia. This project wires them together using **a private Git repo as the middleman**.

---

## How it compares

| | Middleman | Session history | Secret/snapshot encryption | Admin-free autorun |
|---|---|---|---|---|
| **this project** | private Git repo (**no server**) | ✅ **additive `state.db` merge** — both machines keep their own new sessions | ✅ AES-256-CBC + PBKDF2 (snapshots too) | ✅ Startup folder |
| [westsource/agentctxsync](https://github.com/westsource/agentctxsync) | **self-hosted server** (FastAPI + PostgreSQL + web UI + an MCP client per machine) | ✅ via the server | server-side | requires deploying a server |
| [Black-Studio-ia/hermes-sync](https://github.com/Black-Studio-ia/hermes-sync) | Git repo | ❌ no DB merge (and hardcodes `~/.hermes`, so it breaks on Windows; its pull is destructive) | ✅ Fernet | — |
| [TriBrigadeMars/hermes-profile-sync](https://github.com/TriBrigadeMars/hermes-profile-sync) | Git repo | ❌ explicitly does not sync sessions | ❌ | ✅ |
| symlinking SOUL/MEMORY into a cloud drive (common advice) | cloud drive | ❌ "would corrupt, so we don't" | — | — |

The novel bit here is a **serverless, additive, repeatable merge of `state.db`**.

---

## How it works

### 1) File sync — `-Mode push|pull|both`

An explicit **allowlist** is synced (everything else — `tools/`, `bin/`, `cache/` — can be several GB and is never touched):

```
config.yaml, SOUL.md, channel_directory.json
skills/ , memories/ , sessions/
```

- `push`: allowlisted content → staged into the working copy's `payload/` → secrets encrypted → commit → `git pull --rebase` → `git push`
- `pull`: `git pull` → copy `payload/` back **non-destructively** (overwrite/add, **never delete** local files)
- Conflicts: **last writer wins** (the two machines are not required to be byte-identical)

### 2) Encryption

`.env` and `auth.json` are encrypted with **AES-256-CBC + PBKDF2-SHA256 (100k iterations)** into `secrets/*.enc`.
The session snapshot — which contains **raw conversation text** — is encrypted with the same scheme into `snapshots/<label>.db.enc`.
The passphrase comes from `HERMES_SYNC_PASSPHRASE` and must match on both machines; lose it and the encrypted files are gone. **The repo must stay private.**

> With no passphrase the script **refuses** to publish a session snapshot, rather than sending it in the clear.

### 3) Session history merge — `-Mode sessions` (the core)

Sessions live in `state.db` (SQLite/WAL). Copying a live database can yield a corrupt image, and overwriting loses a machine's history. So:

1. **snapshot** — a consistent copy via SQLite's online-backup API (safe while Hermes runs), then encrypted before upload.
2. **transport** — snapshots travel on a dedicated **single-commit branch `sessions-snapshot`, force-pushed every time**, so the repo **never grows** (one file per machine, overwritten, never accumulated in history).
3. **merge** — additive only:
   - sessions are deduplicated by their globally-unique TEXT primary key — existing rows are skipped, never modified;
   - messages are re-inserted **without `id`**, so `AUTOINCREMENT` mints fresh ids and Hermes' own triggers maintain `messages_fts`, `message_uid` and `display_order` — **no manual index rebuild**;
   - **one transaction per session**: the write lock is held for a single session at a time — measured at **470 ms worst case** (a 861-message session), comfortably inside Hermes' own ~1s busy timeout, so a merge cannot stall a running Hermes;
   - `system_prompts` (by hash) and `session_model_usage` come along;
   - machine-local state (`gateway_routing`, `delivery_obligations`, `session_turn_leases`, …) is deliberately skipped.
4. **categorisation** — imported sessions are prefixed with their source: `[desktop] Buy groceries`, `[laptop] …`. Override the label with `HERMES_SYNC_LABEL` (default: computer name).
5. **version safety** — schema drift is checked in **both** directions: a local DB missing a column the snapshot has (this machine's Hermes is older) is refused; so is a snapshot missing a column that is `NOT NULL` with no default. A snapshot missing only optional columns merges with a note. `--allow-schema-skew` forces it.
6. an online-consistent `state.db.bak-<timestamp>` is written before every merge.
7. **legacy** — a plaintext `snapshots/*.db` left behind by an older peer is still read; this version only ever *produces* `.db.enc`.

### 4) Autorun (Windows, no admin)

Registering a Task Scheduler job is routinely denied to non-elevated processes, so a hidden loop is launched from the **per-user Startup folder**:

- every **30 min**: config / skills / memories (`both`)
- every **6 h**: sessions (`sessions`); on failure (network) it **retries every 30 min** and only then returns to the 6 h cadence

---

## Requirements

- **Windows** + Windows PowerShell 5.1+ (the scripts are deliberately ASCII-only so PS 5.1 cannot mangle a BOM-less `.ps1` by decoding it as a legacy codepage)
- **git** (Git for Windows)
- **Python 3.9+** — the script locates an interpreter via PATH → `%LOCALAPPDATA%\hermes\tools\python-*` → the venv inside the Hermes install, and **skips the Microsoft Store alias** (`WindowsApps\python.exe` is a stub that produces nothing)
- a **private** Git repo (GitHub / GitLab / self-hosted)

---

## Quick start

```powershell
# 1) three user-level environment variables (the autorun needs them)
[Environment]::SetEnvironmentVariable('HERMES_SYNC_REMOTE',  'https://github.com/<you>/<repo>.git', 'User')
[Environment]::SetEnvironmentVariable('HERMES_SYNC_PASSPHRASE', '<strong-passphrase>', 'User')
[Environment]::SetEnvironmentVariable('HERMES_SYNC_LABEL',    'laptop', 'User')   # optional

# 2) drop the four scripts into  %LOCALAPPDATA%\hermes\sync\

# 3) first sync (one interactive GitHub login, then the credential is cached)
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\hermes\sync\sync-hermes.ps1" -Mode push

# 4) on the other machine
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\hermes\sync\sync-hermes.ps1" -Mode pull

# 5) sessions: publish this machine's encrypted snapshot and merge the peers'
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\hermes\sync\sync-hermes.ps1" -Mode sessions

# 6) autorun: drop HermesSync.cmd into the Startup folder (Win+R -> shell:startup)
```

**If git cannot reach GitHub** (common behind a system proxy): Clash/V2Ray-style system proxies are *not* used by git, so configure it explicitly:

```powershell
git config --global http.proxy http://127.0.0.1:7890   # your proxy port
git config --global https.proxy http://127.0.0.1:7890
```

---

## Tests

```bash
python -m unittest discover -s tests -v     # no third-party deps; 9 cases
```

The suite materialises the **real Hermes `state.db` schema** (`tests/fixtures/schema.sql` — DDL only, no data) and drives snapshot → merge with synthetic data, asserting: additivity, exact row counts, FTS integrity, tags that are never applied twice, idempotency, that **both** drift directions fail loudly, and that a snapshot taken while a writer is committing is still consistent.

CI (GitHub Actions) runs the same suite on Python 3.9/3.12/3.13 and parses the `.ps1` files.

---

## Known limitations (honest version)

- **Windows-tested only.** Path handling assumes `HERMES_HOME` / `%LOCALAPPDATA%`; other platforms need changes.
- **CI cannot cover the PowerShell runtime** — a Linux runner only parse-checks it, so PS 5.1 encoding/path pitfalls still need a human run.
- **File sync is last-writer-wins**, not line-level merging.
- **The merge is coupled to Hermes' `state.db` schema.** On drift it *refuses* rather than corrupting; it needs updating when the schema moves.
- **The write-lock bound is "the single largest session"** (measured 470 ms for 861 messages). A session with thousands of messages could exceed 1 s; `messages` has no unique constraint, so sub-session chunking cannot be made idempotent without a duplication hazard.
- Imported sessions may need a Hermes refresh/restart to appear (the gateway caches).
- The snapshot is the **whole DB** (system prompts included), tens of MB — a dedicated force-pushed branch keeps the repo from growing, but that is the per-round transfer size.
- Force-pushing leaves unreachable objects on the remote until GitHub's GC collects them (~2 weeks).
- Hermes' other machine-local DBs (`kanban.db`, `cron/executions.db`) are not handled — live SQLite is never touched.
- Encrypted snapshots mean a new machine needs the **same passphrase** before it can receive history.

Design notes, the pre-release interrogation (measured facts, decision log, residual risks) live in [GRILL.md](GRILL.md).

---

## Files

| File | Purpose |
|---|---|
| `sync-hermes.ps1` | engine: `push` / `pull` / `both` / `sessions` — AES, allowlist, encrypted snapshot publishing |
| `state_db_tool.py` | session store: `snapshot` (online backup) / `merge` (additive, per-session transactions, two-way schema check) |
| `sync-loop.ps1` | hidden background loop (30 min files / 6 h sessions + retry) |
| `HermesSync.cmd` | Startup-folder launcher |
| `tests/` | 9 cases + a real-schema DDL fixture |
| `GRILL.md` | pre-release interrogation: facts, decisions, risks, verification experiments |

---

## License

MIT — see [LICENSE](LICENSE).

**Disclaimer:** unofficial, may break as Hermes evolves, and it merges your real session store — make sure the `state.db.bak-*` backups are present before relying on it.
