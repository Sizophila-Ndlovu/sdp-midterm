"""SQLite storage: schema, connections, and small helpers."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RAT_DATA_DIR", BASE_DIR / "data"))
REPOS_DIR = DATA_DIR / "repos"
DB_PATH = DATA_DIR / "rat.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN ('zip', 'url')),
  source TEXT NOT NULL,
  git_dir TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ingesting',
  progress REAL NOT NULL DEFAULT 0,
  message TEXT,
  head_sha TEXT,
  commit_count INTEGER NOT NULL DEFAULT 0,
  has_mailmap INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  UNIQUE (repo_id, name, email)
);

-- raw author pair (name, email) -> canonical author; mailmap identity entries plus
-- manual merges both live here
CREATE TABLE IF NOT EXISTS author_map (
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  raw_name TEXT NOT NULL,
  raw_email TEXT NOT NULL,
  author_id INTEGER NOT NULL,
  PRIMARY KEY (repo_id, raw_name, raw_email)
);

CREATE TABLE IF NOT EXISTS commits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  sha TEXT NOT NULL,
  raw_name TEXT NOT NULL,
  raw_email TEXT NOT NULL,
  author_id INTEGER NOT NULL,
  committer_ts INTEGER NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  UNIQUE (repo_id, sha)
);

-- one row per (commit, file) with a real line change; binary files and zero-change
-- renames are never stored. Renames are stored under the new path.
CREATE TABLE IF NOT EXISTS file_changes (
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  commit_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  added INTEGER NOT NULL,
  removed INTEGER NOT NULL
);

-- one row per (commit, directory) that the commit touched at least one file under;
-- dir '' is the repository root. Makes modification counts exact and fast.
CREATE TABLE IF NOT EXISTS dir_commits (
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  commit_id INTEGER NOT NULL,
  dir TEXT NOT NULL,
  PRIMARY KEY (repo_id, dir, commit_id)
) WITHOUT ROWID;

-- files present at HEAD, so objects with zero metrics are still listed/selectable
CREATE TABLE IF NOT EXISTS files (
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  PRIMARY KEY (repo_id, path)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_commits_ts ON commits(repo_id, committer_ts);
CREATE INDEX IF NOT EXISTS idx_commits_author ON commits(repo_id, author_id);
CREATE INDEX IF NOT EXISTS idx_fc_path ON file_changes(repo_id, path);
CREATE INDEX IF NOT EXISTS idx_fc_commit ON file_changes(commit_id);
"""


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(DB_PATH, timeout=60)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA synchronous=NORMAL")
    cx.execute("PRAGMA foreign_keys=ON")
    return cx


@contextmanager
def db():
    cx = connect()
    try:
        yield cx
        cx.commit()
    except Exception:
        cx.rollback()
        raise
    finally:
        cx.close()


def init_db():
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    with db() as cx:
        cx.executescript(SCHEMA)


def list_repos(cx):
    rows = cx.execute(
        "SELECT id, name, source_type, source, status, progress, message, head_sha,"
        " commit_count, has_mailmap, created_at FROM repos ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_repo(cx, repo_id):
    row = cx.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return dict(row) if row else None


def delete_repo(cx, repo_id):
    cx.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
