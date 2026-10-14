#!/usr/bin/env python3
"""Hermes session-store (state.db) snapshot + additive merge tool.

snapshot : consistent online copy of the live state.db (safe while Hermes runs)
merge    : additively merge a snapshot into the local state.db
           - only ADDS rows, never updates/deletes existing local rows
           - sessions matched by their TEXT primary key (globally unique)
           - messages re-inserted without id so AUTOINCREMENT mints fresh ids
             (FTS/uid/display_order triggers then fire automatically)
           - --tag prefixes the inserted sessions' display_name/title so sessions
             from another machine stay categorised in the session list
           - one transaction PER SESSION, so the SQLite write lock is never held for more
             than a single session and a live Hermes never stalls past its ~1s busy timeout
           - checks schema drift in BOTH directions and refuses rather than guessing
           - optional --refuse-if-active aborts while Hermes looks active (gateway pid / WAL)

Tables touched: sessions, messages, system_prompts, session_model_usage.
Machine-local routing/runtime tables are deliberately skipped.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def hermes_home(explicit):
    if explicit:
        return Path(explicit)
    h = os.environ.get("HERMES_HOME")
    if not h:
        local = os.environ.get("LOCALAPPDATA")
        h = os.path.join(local, "hermes") if local else os.path.expanduser("~/.hermes")
    return Path(h)


def cols(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def col_safe_to_omit(conn, table, name):
    """True when INSERTing a row without this column cannot fail or misbehave."""
    for _cid, cname, _typ, notnull, dflt, pk in conn.execute("PRAGMA table_info(%s)" % table):
        if cname == name:
            return (not notnull) or (dflt is not None) or bool(pk)
    return True


def table_exists(conn, table):
    return conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone() is not None


def pid_alive(pid):
    if os.name != "nt":                      # POSIX (used by the CI runner)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid],
            capture_output=True, text=True, errors="ignore", timeout=25,
        ).stdout
        return str(pid) in out
    except Exception:
        return True


def hermes_active(home):
    """Return a reason string if Hermes looks active, else None."""
    pidf = home / "gateway.pid"
    try:
        if pidf.exists():
            pid = json.loads(pidf.read_text(encoding="utf-8-sig")).get("pid")
            if pid and pid_alive(int(pid)):
                return "gateway running (pid %s)" % pid
    except Exception:
        pass
    wal = home / "state.db-wal"
    try:
        if wal.exists() and (time.time() - wal.stat().st_mtime) < 90:
            return "state.db-wal written within the last 90s"
    except Exception:
        pass
    return None


def cmd_snapshot(args):
    home = hermes_home(args.home)
    src = home / "state.db"
    if not src.exists():
        print("ERROR: no state.db at %s" % src)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    src_conn = sqlite3.connect("file:%s?mode=ro" % src.as_posix(), uri=True)
    dst_conn = sqlite3.connect(str(out))
    src_conn.backup(dst_conn)          # consistent online snapshot
    dst_conn.close()
    src_conn.close()
    print("snapshot written: %s (%d bytes)" % (out, out.stat().st_size))
    return 0


def cmd_merge(args):
    home = hermes_home(args.home)
    db = home / "state.db"
    snap_path = Path(args.snapshot)
    if not db.exists():
        print("ERROR: no local state.db at %s" % db)
        return 1
    if not snap_path.exists():
        print("ERROR: snapshot not found: %s" % snap_path)
        return 1

    active = hermes_active(home)
    if active:
        if args.refuse_if_active:
            print("REFUSED: Hermes appears active (%s)." % active)
            print("Close Hermes and retry, or drop --refuse-if-active.")
            return 2
        print("NOTE: Hermes appears active (%s); merging anyway. SQLite serialises the write,"
              " the backup below is an online-consistent copy, and the new sessions appear on a"
              " Hermes refresh/restart." % active)

    # online-consistent backup (safe even while Hermes is running)
    bak = db.with_name("state.db.bak-" + time.strftime("%Y%m%d-%H%M%S"))
    _bsrc = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    _bdst = sqlite3.connect(str(bak))
    _bsrc.backup(_bdst)
    _bdst.close()
    _bsrc.close()
    print("backup written: %s" % bak)

    loc = sqlite3.connect(str(db), timeout=60)
    loc.execute("PRAGMA foreign_keys=OFF")
    snap = sqlite3.connect("file:%s?mode=ro" % snap_path.as_posix(), uri=True)

    try:
        for t in ("sessions", "messages"):
            if not table_exists(loc, t) or not table_exists(snap, t):
                print("ERROR: table %s missing on one side" % t)
                return 1
            s_cols_all, l_cols_all = set(cols(snap, t)), set(cols(loc, t))
            extra = s_cols_all - l_cols_all          # snapshot NEWER than local
            missing = l_cols_all - s_cols_all        # snapshot OLDER than local
            if extra:
                print("ERROR: local %s lacks column(s) %s - this machine's Hermes is older,"
                      " aborting." % (t, sorted(extra)))
                print("       Upgrade Hermes here, or pass --allow-schema-skew.")
                return 1
            if missing:
                unsafe = [c for c in sorted(missing) if not col_safe_to_omit(loc, t, c)]
                if unsafe and not args.allow_schema_skew:
                    print("ERROR: snapshot %s lacks column(s) %s that this machine has, and"
                          " %s cannot be safely omitted (NOT NULL, no default) - aborting."
                          % (t, sorted(missing), unsafe))
                    print("       The other machine likely runs an older Hermes: upgrade it, or"
                          " pass --allow-schema-skew to merge the common columns.")
                    return 1
                print("NOTE: snapshot %s lacks column(s) %s; merging the common columns."
                      % (t, sorted(missing)))

        local_sessions = {r[0] for r in loc.execute("select id from sessions")}
        snap_sessions = {r[0] for r in snap.execute("select id from sessions")}
        todo = snap_sessions - local_sessions

        # include parent sessions so resumed/compressed chains stay intact
        frontier = set(todo)
        while frontier:
            more = set()
            for sid in frontier:
                row = snap.execute(
                    "select parent_session_id from sessions where id=?", (sid,)
                ).fetchone()
                if row and row[0] and row[0] in snap_sessions \
                        and row[0] not in local_sessions and row[0] not in todo:
                    more.add(row[0])
            todo |= more
            frontier = more

        print("sessions in snapshot: %d | already local: %d | to add: %d"
             % (len(snap_sessions), len(snap_sessions & local_sessions), len(todo)))
        if not todo:
            print("nothing to merge")
            return 0

        s_cols = [c for c in cols(snap, "sessions") if c in set(cols(loc, "sessions"))]
        m_cols = [c for c in cols(snap, "messages") if c in set(cols(loc, "messages"))]
        m_ins = [c for c in m_cols if c != "id"]
        s_ph = ",".join("?" * len(s_cols))
        m_ph = ",".join("?" * len(m_ins))

        added_sessions = added_messages = 0
        worst_ms = 0.0
        for sid in sorted(todo):
            t0 = time.time()
            vals = dict(zip(s_cols, snap.execute(
                "select %s from sessions where id=?" % ",".join(s_cols), (sid,)
            ).fetchone()))
            if args.tag:
                base = str(vals.get("display_name") or vals.get("title") or sid)
                if not base.startswith("[%s]" % args.tag):
                    vals["display_name"] = "[%s] %s" % (args.tag, base)
                t = str(vals.get("title") or "")
                if t and not t.startswith("[%s]" % args.tag):
                    vals["title"] = "[%s] %s" % (args.tag, t)
            loc.execute(
                "insert into sessions (%s) values (%s)" % (",".join(s_cols), s_ph),
                [vals[c] for c in s_cols],
            )
            added_sessions += 1
            for mrow in snap.execute(
                "select %s from messages where session_id=? order by id" % ",".join(m_cols),
                (sid,),
            ):
                mv = dict(zip(m_cols, mrow))
                loc.execute(
                    "insert into messages (%s) values (%s)" % (",".join(m_ins), m_ph),
                    [mv[c] for c in m_ins],
                )
                added_messages += 1

            if table_exists(loc, "system_prompts") and table_exists(snap, "system_prompts"):
                h = vals.get("system_prompt_hash")
                if h and not loc.execute(
                    "select 1 from system_prompts where hash=?", (h,)
                ).fetchone():
                    r = snap.execute(
                        "select hash,prompt from system_prompts where hash=?", (h,)
                    ).fetchone()
                    if r:
                        loc.execute("insert or ignore into system_prompts (hash,prompt) values (?,?)", r)

            if table_exists(loc, "session_model_usage") and table_exists(snap, "session_model_usage"):
                u_cols = [c for c in cols(snap, "session_model_usage")
                          if c in set(cols(loc, "session_model_usage"))]
                for r in snap.execute(
                    "select %s from session_model_usage where session_id=?" % ",".join(u_cols),
                    (sid,),
                ):
                    loc.execute(
                        "insert or ignore into session_model_usage (%s) values (%s)"
                        % (",".join(u_cols), ",".join("?" * len(u_cols))),
                        r,
                    )

            # One transaction PER SESSION. The SQLite write lock is therefore held for a single
            # session at a time, so importing 200 sessions can no longer stall a live Hermes
            # past its ~1s busy timeout. The merge stays idempotent, so an interrupted run
            # simply continues on the next call.
            loc.commit()
            worst_ms = max(worst_ms, (time.time() - t0) * 1000.0)

        print("merged: %d session(s), %d message(s) added" % (added_sessions, added_messages))
        print("longest single write transaction: %.0f ms" % worst_ms)
        return 0
    except Exception:
        loc.rollback()
        raise
    finally:
        loc.close()
        snap.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("snapshot", help="consistent copy of the live state.db")
    s1.add_argument("--out", required=True)
    s1.add_argument("--home")
    s1.set_defaults(func=cmd_snapshot)

    s2 = sub.add_parser("merge", help="additively merge a snapshot into the local state.db")
    s2.add_argument("--snapshot", required=True)
    s2.add_argument("--home")
    s2.add_argument("--tag", help="label prefixed to imported sessions (e.g. source machine)")
    s2.add_argument("--refuse-if-active", action="store_true",
                    help="abort instead of merging while Hermes looks active")
    s2.add_argument("--allow-schema-skew", action="store_true",
                    help="merge the common columns even when the snapshot is older than the"
                         " local schema (default: abort unless the missing columns are optional)")
    s2.set_defaults(func=cmd_merge)

    a = p.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
