"""One persistent SQLite database; every writer uses an immediate transaction."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid():
    return uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, handle TEXT UNIQUE, password TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
 provider TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users,
 PRIMARY KEY(provider,subject)
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users,
 csrf TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS spaces (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
 space_id TEXT NOT NULL REFERENCES spaces, user_id TEXT NOT NULL REFERENCES users,
 PRIMARY KEY(space_id,user_id)
);
CREATE TABLE IF NOT EXISTS invites (
 token_hash TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces,
 expires INTEGER NOT NULL, used_by TEXT REFERENCES users
);
CREATE TABLE IF NOT EXISTS questions (
 id TEXT PRIMARY KEY, author_id TEXT NOT NULL REFERENCES users,
 scope TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
 conditions TEXT NOT NULL, attempts TEXT NOT NULL, need TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('question','knowledge')),
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS questions_scope ON questions(scope,created_at);
CREATE TABLE IF NOT EXISTS answers (
 id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES questions,
 author_id TEXT NOT NULL REFERENCES users, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
 id TEXT PRIMARY KEY, answer_id TEXT NOT NULL REFERENCES answers,
 number INTEGER NOT NULL, body TEXT NOT NULL, conditions TEXT NOT NULL,
 evidence TEXT NOT NULL, observed_at TEXT NOT NULL, reason TEXT NOT NULL,
 ai_assisted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(answer_id,number)
);
CREATE TABLE IF NOT EXISTS sources (
 target_question TEXT REFERENCES questions, target_revision TEXT REFERENCES revisions,
 source_revision TEXT NOT NULL REFERENCES revisions,
 CHECK((target_question IS NULL) != (target_revision IS NULL)),
 UNIQUE(target_question,source_revision), UNIQUE(target_revision,source_revision)
);
CREATE TABLE IF NOT EXISTS selections (
 id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES questions,
 revision_id TEXT NOT NULL REFERENCES revisions, user_id TEXT NOT NULL REFERENCES users,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contributions (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES revisions,
 author_id TEXT NOT NULL REFERENCES users,
 kind TEXT NOT NULL CHECK(kind IN ('addition','correction','clarification')),
 body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS acknowledgments (
 revision_id TEXT NOT NULL REFERENCES revisions,
 contribution_id TEXT NOT NULL REFERENCES contributions,
 PRIMARY KEY(revision_id,contribution_id)
);
CREATE TABLE IF NOT EXISTS outcomes (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES revisions,
 user_id TEXT NOT NULL REFERENCES users,
 result TEXT NOT NULL CHECK(result IN ('solved','partial','failed')),
 conditions TEXT NOT NULL, body TEXT NOT NULL, observed_at TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(revision_id,user_id)
);
CREATE TABLE IF NOT EXISTS reuses (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES revisions,
 user_id TEXT NOT NULL REFERENCES users, note TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(revision_id,user_id)
);
CREATE TABLE IF NOT EXISTS bookmarks (
 user_id TEXT NOT NULL REFERENCES users, revision_id TEXT NOT NULL REFERENCES revisions,
 PRIMARY KEY(user_id,revision_id)
);
CREATE TABLE IF NOT EXISTS ledger (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users,
 amount INTEGER NOT NULL, kind TEXT NOT NULL, reference TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(user_id,kind,reference)
);
CREATE TABLE IF NOT EXISTS stakes (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES revisions,
 user_id TEXT NOT NULL REFERENCES users, amount INTEGER NOT NULL CHECK(amount>0),
 created_at TEXT NOT NULL, withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users, scope TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations,
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), body TEXT NOT NULL,
 created_at TEXT NOT NULL
);
"""


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


@contextmanager
def transaction(path):
    db = connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def one(db, sql, args=()):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row else None


def all_rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def new_user(db, name, handle=None, password=None):
    user_id = uid()
    db.execute("INSERT INTO users VALUES (?,?,?,?,?)", (user_id, name, handle, password, now()))
    db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)",
               (uid(), user_id, 100, "pilot_grant", user_id, now()))
    return one(db, "SELECT * FROM users WHERE id=?", (user_id,))
