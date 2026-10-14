#!/usr/bin/env python3
"""Tests for state_db_tool.py.

A REAL Hermes state.db schema (tests/fixtures/schema.sql — DDL only, no real data) is
materialised into a temp dir, filled with SYNTHETIC sessions/messages, and then
exercised end-to-end through the CLI. No third-party dependencies:

    python -m unittest discover -s tests -v
"""
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "state_db_tool.py"
SCHEMA = Path(__file__).resolve().parent / "fixtures" / "schema.sql"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL)] + [str(a) for a in args],
                          capture_output=True, text=True)


def open_db(path):
    return sqlite3.connect(str(path), timeout=30)


def build_db(path, n_sessions=3, msgs_per=4):
    """Materialise the real schema and fill it with synthetic data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = open_db(path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    seed(conn, n_sessions, msgs_per)
    return conn


def seed(conn, n_sessions, msgs_per, prefix="s"):
    for s in range(n_sessions):
        sid = "%s%02d" % (prefix, s)
        conn.execute(
            "insert into sessions (id, source, started_at, display_name, message_count)"
            " values (?,?,?,?,?)", (sid, "cli", 1000.0 + s, "session %s" % sid, msgs_per))
        for m in range(msgs_per):
            conn.execute(
                "insert into messages (session_id, role, content, timestamp)"
                " values (?,?,?,?)",
                (sid, "user" if m % 2 == 0 else "assistant",
                 "alpha%s beta%d gamma" % (sid, m), 1001.0 + m))
    conn.commit()
    return conn


def counts(db):
    """(sessions, messages) — opens/closes its own connection (Windows keeps files locked)."""
    conn = open_db(db)
    try:
        return (conn.execute("select count(*) from sessions").fetchone()[0],
                conn.execute("select count(*) from messages").fetchone()[0])
    finally:
        conn.close()


def scalar(db, sql):
    conn = open_db(db)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def merge(self, snapshot, tag=None, *extra):
        args = ["merge", "--snapshot", snapshot, "--home", self.home]
        if tag:
            args += ["--tag", tag]
        return run(*args, *extra)

    def snapshot(self, out):
        return run("snapshot", "--out", out, "--home", self.home)

    def assert_healthy(self, db):
        self.assertEqual(scalar(db, "pragma integrity_check"), "ok")
        conn = open_db(db)
        try:
            conn.execute("insert into messages_fts(messages_fts) values('integrity-check')")
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ tests
    def test_merge_restores_deleted_sessions_only(self):
        db = self.home / "state.db"
        conn = build_db(db, 4, 5)
        # a machine that already has 2 of the 4 sessions locally
        conn.execute("delete from messages where session_id in ('s02','s03')")
        conn.execute("delete from sessions where id in ('s02','s03')")
        conn.commit()
        conn.close()
        before_msgs = counts(db)[1]

        snap = Path(self.tmp.name) / "peer.db"
        build_db(snap, 4, 5).close()
        res = self.merge(snap, tag="PEER")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("to add: 2", res.stdout)
        self.assertIn("merged: 2 session(s), 10 message(s) added", res.stdout)

        self.assertEqual(counts(db), (4, 20))
        self.assertGreater(counts(db)[1], before_msgs)

        conn = open_db(db)
        try:
            tagged = [r[0] for r in conn.execute(
                "select id from sessions where display_name like '[PEER] %' order by id")]
            self.assertEqual(tagged, ["s02", "s03"])
            self.assertEqual(
                conn.execute("select display_name from sessions where id='s00'").fetchone()[0],
                "session s00")
            self.assertEqual(conn.execute(
                "select count(*) from messages where session_id in ('s02','s03')"
                " and (message_uid is null or display_order is null)").fetchone()[0], 0)
        finally:
            conn.close()
        self.assert_healthy(db)

    def test_merge_is_idempotent(self):
        db = self.home / "state.db"
        build_db(db, 3, 3).close()
        snap = Path(self.tmp.name) / "peer.db"
        peer = build_db(snap, 3, 3)
        seed(peer, 2, 4, prefix="other")
        peer.close()

        first = self.merge(snap, tag="PEER")
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertIn("merged: 2 session(s)", first.stdout)
        after_first = counts(db)

        second = self.merge(snap, tag="PEER")
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("nothing to merge", second.stdout)
        self.assertEqual(counts(db), after_first)
        self.assert_healthy(db)

    def test_tag_is_not_applied_twice(self):
        src = self.home / "state.db"
        build_db(src, 2, 2).close()
        peer = Path(self.tmp.name) / "peer.db"
        build_db(peer, 2, 2).close()

        r1 = self.merge(peer, tag="PEER")
        self.assertEqual(r1.returncode, 0, r1.stdout)

        # snapshot the now-tagged db, merge it into a fresh machine with the same tag
        tagged_snap = Path(self.tmp.name) / "tagged.db"
        self.assertEqual(self.snapshot(tagged_snap).returncode, 0)

        fresh = Path(self.tmp.name) / "fresh"
        build_db(fresh / "state.db", 0, 0).close()
        r2 = run("merge", "--snapshot", tagged_snap, "--home", fresh, "--tag", "PEER")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        conn = open_db(fresh / "state.db")
        try:
            names = [r[0] for r in conn.execute("select display_name from sessions")]
        finally:
            conn.close()
        self.assertTrue(names)
        for n in names:
            self.assertNotIn("[PEER] [PEER]", n)
            self.assertTrue(n.startswith("[PEER] "), n)

    def test_merge_reports_bounded_write_transaction(self):
        db = self.home / "state.db"
        build_db(db, 1, 50).close()
        peer = Path(self.tmp.name) / "peer.db"
        build_db(peer, 2, 60).close()
        res = self.merge(peer, tag="PEER")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("longest single write transaction:", res.stdout)

    def test_snapshot_is_consistent_while_a_writer_is_active(self):
        db = self.home / "state.db"
        conn = build_db(db, 1, 50)
        conn.execute("pragma journal_mode=wal")
        conn.commit()
        stop = threading.Event()

        def writer():
            w = open_db(db)
            i = 0
            try:
                while not stop.is_set():
                    i += 1
                    try:
                        w.execute(
                            "insert into messages (session_id, role, content, timestamp)"
                            " values ('s00','assistant',?,?)",
                            ("streamed %d" % i, time.time()))
                        w.commit()
                    except sqlite3.Error:
                        pass
                    time.sleep(0.001)
            finally:
                w.close()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        time.sleep(0.2)
        snap = Path(self.tmp.name) / "live-snapshot.db"
        res = run("snapshot", "--out", snap, "--home", self.home)
        stop.set()
        t.join(timeout=5)
        conn.close()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(scalar(snap, "pragma integrity_check"), "ok")
        self.assertGreater(scalar(snap, "select count(*) from messages"), 0)


BASE_SESSIONS = ("id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,"
                 " display_name TEXT")
MINI_MESSAGES = ("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,"
                 " timestamp REAL NOT NULL);")


class SchemaDriftTests(unittest.TestCase):
    """Both drift directions must fail loudly instead of guessing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self, name, sessions_ddl, session_values):
        p = self.d / name / "state.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p))
        try:
            conn.executescript("CREATE TABLE sessions (%s);\n%s"
                               % (sessions_ddl, MINI_MESSAGES))
            conn.execute("insert into sessions values (%s)" % session_values)
            conn.execute("insert into messages (session_id, role, content, timestamp)"
                         " values ('a','user','x',1.0)")
            conn.commit()
        finally:
            conn.close()
        return p

    def test_snapshot_newer_than_local_is_rejected(self):
        snap = self._db("peer", BASE_SESSIONS + ", brand_new TEXT",
                        "'a','cli',1.0,'a',NULL")
        loc = self._db("local", BASE_SESSIONS, "'a','cli',1.0,'a'")
        res = run("merge", "--snapshot", snap, "--home", loc.parent)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("older", res.stdout)
        self.assertIn("brand_new", res.stdout)

    def test_snapshot_older_than_local_is_rejected_when_column_is_required(self):
        snap = self._db("peer", BASE_SESSIONS, "'a','cli',1.0,'a'")
        loc = self._db("local", BASE_SESSIONS + ", must_have TEXT NOT NULL",
                       "'a','cli',1.0,'a','x'")
        res = run("merge", "--snapshot", snap, "--home", loc.parent)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("cannot be safely omitted", res.stdout)
        self.assertIn("must_have", res.stdout)

    def test_older_snapshot_with_only_optional_extras_is_merged_with_a_note(self):
        snap = self._db("peer", BASE_SESSIONS, "'a','cli',1.0,'a'")
        loc = self._db("local", BASE_SESSIONS + ", extra_session TEXT",
                       "'a','cli',1.0,'a',NULL")
        res = run("merge", "--snapshot", snap, "--home", loc.parent, "--tag", "PEER")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("merging the common columns", res.stdout)

    def test_older_snapshot_is_accepted_with_allow_schema_skew(self):
        snap = self._db("peer", BASE_SESSIONS, "'a','cli',1.0,'a'")
        loc = self._db("local", BASE_SESSIONS + ", must_have TEXT NOT NULL",
                       "'a','cli',1.0,'a','x'")
        res = run("merge", "--snapshot", snap, "--home", loc.parent, "--allow-schema-skew")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
