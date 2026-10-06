"""Ingestion: zip upload or clone URL -> parsed history rows in SQLite.

History is read through the git CLI in one streaming pass:

    git log --no-merges --numstat -M50% --format=<NUL-safe header fields>

Non-merge commits only, committer dates, rename detection at 50% (edits land on the
new path), binary files skipped (numstat '-'), deletions recorded on their path.
"""
import logging
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import metrics
import storage

log = logging.getLogger(__name__)

# \x01 marks a commit header line; \x1f separates its fields
LOG_FORMAT = "%x01%H%x1f%P%x1f%an%x1f%ae%x1f%aN%x1f%aE%x1f%ct%x1f%s"
FLUSH_EVERY_COMMITS = 1000
FLUSH_EVERY_ROWS = 20000


class IngestError(Exception):
    pass


def _run(git_dir, *args, timeout=None):
    return subprocess.run(["git", "-C", str(git_dir), *args],
                          capture_output=True, text=True, timeout=timeout)


def start_url(url):
    """Deep-clone `url` (bare, full history) in a worker thread. Returns the repo id."""
    name = Path(urlparse(url).path).name or "repository"
    if name.endswith(".git"):
        name = name[:-4]
    repo_id = _new_repo("url", url, name)
    threading.Thread(target=_url_worker, args=(repo_id, url), daemon=True,
                     name=f"ingest-{repo_id}").start()
    return repo_id


def start_zip(upload_path, filename):
    """Store and analyse an uploaded zip in a worker thread. Returns the repo id."""
    repo_id = _new_repo("zip", filename, Path(filename).stem or "repository")
    dest = storage.REPOS_DIR / str(repo_id)
    dest.mkdir(parents=True, exist_ok=True)
    saved = dest / "upload.zip"
    shutil.move(str(upload_path), saved)
    threading.Thread(target=_zip_worker, args=(repo_id, saved), daemon=True,
                     name=f"ingest-{repo_id}").start()
    return repo_id


def remove_files(repo_id):
    shutil.rmtree(storage.REPOS_DIR / str(repo_id), ignore_errors=True)
    shutil.rmtree(storage.REPOS_DIR / f"{repo_id}.git", ignore_errors=True)


def _new_repo(source_type, source, name):
    with storage.db() as cx:
        cur = cx.execute("INSERT INTO repos (name, source_type, source) VALUES (?, ?, ?)",
                         (name, source_type, source))
        return cur.lastrowid


def _set_git_dir(repo_id, git_dir):
    with storage.db() as cx:
        cx.execute("UPDATE repos SET git_dir = ? WHERE id = ?", (str(git_dir), repo_id))


def _fail(repo_id, exc):
    log.exception("ingest failed for repo %s", repo_id)
    with storage.db() as cx:
        cx.execute("UPDATE repos SET status = 'error', message = ? WHERE id = ?",
                   (str(exc)[:1000] or type(exc).__name__, repo_id))


def _url_worker(repo_id, url):
    try:
        dest = storage.REPOS_DIR / f"{repo_id}.git"
        proc = subprocess.run(["git", "clone", "--bare", "--quiet", "--", url, str(dest)],
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            raise IngestError("clone failed: " + (err[-1] if err else "unknown error"))
        _set_git_dir(repo_id, dest)
        _analyze(repo_id, dest)
    except Exception as exc:  # reported to the user through the repo status
        _fail(repo_id, exc)


def _zip_worker(repo_id, zip_path):
    try:
        dest = storage.REPOS_DIR / str(repo_id) / "repo"
        _extract(zip_path, dest)
        zip_path.unlink(missing_ok=True)
        root = _find_repo_root(dest)
        if root is None:
            raise IngestError("no .git directory or gitdir file found in the zip "
                              "(export the repository including its history)")
        _set_git_dir(repo_id, root)
        _analyze(repo_id, root)
    except Exception as exc:  # reported to the user through the repo status
        _fail(repo_id, exc)


def _extract(zip_path, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            p = Path(member.filename)
            if p.is_absolute() or ".." in p.parts:
                raise IngestError(f"unsafe path in zip: {member.filename!r}")
        zf.extractall(dest)


def _find_repo_root(base, max_depth=2):
    """Find the directory holding .git (zips often wrap the repo in a folder)."""
    queue = [(base, 0)]
    while queue:
        d, depth = queue.pop(0)
        if (d / ".git").exists() and _run(d, "rev-parse", "--git-dir").returncode == 0:
            return d
        if depth < max_depth:
            for child in sorted(d.iterdir()):
                if child.is_dir() and child.name not in (".git", "__MACOSX"):
                    queue.append((child, depth + 1))
    return None


def _analyze(repo_id, git_dir):
    git_dir = Path(git_dir)
    head = _run(git_dir, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise IngestError("repository has no commits (unborn HEAD)")
    head_sha = head.stdout.strip()
    total = int(_run(git_dir, "rev-list", "--no-merges", "--count", "HEAD").stdout.strip() or 0)
    has_mailmap = 1 if _run(git_dir, "cat-file", "-e", "HEAD:.mailmap").returncode == 0 else 0

    cx = storage.connect()
    try:
        cx.execute("UPDATE repos SET head_sha = ?, has_mailmap = ? WHERE id = ?",
                   (head_sha, has_mailmap, repo_id))
        cx.commit()

        stderr_sink = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            ["git", "-C", str(git_dir), "-c", "core.quotePath=false", "log",
             "--use-mailmap", "--no-merges", "--numstat", "-M50%",
             f"--format={LOG_FORMAT}"],
            stdout=subprocess.PIPE, stderr=stderr_sink,
            text=True, encoding="utf-8", errors="replace", bufsize=1 << 20)

        state = _State(cx, repo_id)
        count = 0
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                if line[0] == "\x01":
                    state.commit_header(line[1:])
                    count += 1
                    if count % FLUSH_EVERY_COMMITS == 0:
                        state.flush(progress=(count / total if total else 0.0), count=count)
                else:
                    state.change(line)
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            stderr_sink.seek(0)
            err = stderr_sink.read().decode("utf-8", "replace").strip()
            raise IngestError("git log failed: " + (err.splitlines()[-1] if err else f"exit code {rc}"))
        state.flush()

        _load_files(cx, repo_id, git_dir)
        cx.execute("UPDATE repos SET status = 'ready', progress = 1, message = NULL,"
                   " commit_count = ? WHERE id = ?", (count, repo_id))
        cx.execute("ANALYZE")  # fresh stats so the planner picks index-driven plans
        cx.commit()
        log.info("repo %s (%s) ingested: %s commits", repo_id, git_dir, count)
    finally:
        cx.close()

    metrics.invalidate(repo_id)


class _State:
    """Accumulates parsed commits/file changes and flushes them to SQLite in batches."""

    def __init__(self, cx, repo_id):
        self.cx = cx
        self.repo_id = repo_id
        self.author_cache = {}
        self.commit_id = None
        self.commit_dirs = set()
        self.changes = []   # (commit_id, path, added, removed)
        self.dirs = []      # (commit_id, dir)

    def commit_header(self, header):
        sha, _parents, raw_name, raw_email, canon_name, canon_email, ts, summary = \
            header.split("\x1f", 7)
        author_id = self._author(raw_name, raw_email, canon_name, canon_email)
        cur = self.cx.execute(
            "INSERT INTO commits (repo_id, sha, raw_name, raw_email, author_id,"
            " committer_ts, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.repo_id, sha, raw_name, raw_email, author_id, int(ts), summary))
        self.commit_id = cur.lastrowid
        self.commit_dirs = set()

    def change(self, line):
        parts = line.split("\t", 2)
        if len(parts) != 3:
            return
        added, removed, path = parts
        if added == "-" or removed == "-":
            return  # binary file (git's own detection)
        added, removed = int(added), int(removed)
        if added == 0 and removed == 0:
            return  # pure rename / mode change: no metric effect
        path = _resolve_path(path)
        self.changes.append((self.commit_id, path, added, removed))
        for d in _ancestor_dirs(path):
            if d not in self.commit_dirs:
                self.commit_dirs.add(d)
                self.dirs.append((self.commit_id, d))
        if len(self.changes) >= FLUSH_EVERY_ROWS:
            self.flush()

    def flush(self, progress=None, count=None):
        if self.changes:
            self.cx.executemany(
                "INSERT INTO file_changes (repo_id, commit_id, path, added, removed)"
                " VALUES (?, ?, ?, ?, ?)",
                [(self.repo_id, cid, p, a, r) for cid, p, a, r in self.changes])
            self.changes.clear()
        if self.dirs:
            self.cx.executemany(
                "INSERT OR IGNORE INTO dir_commits (repo_id, commit_id, dir) VALUES (?, ?, ?)",
                [(self.repo_id, cid, d) for cid, d in self.dirs])
            self.dirs.clear()
        if progress is not None:
            self.cx.execute("UPDATE repos SET progress = ?, commit_count = ? WHERE id = ?",
                            (progress, count, self.repo_id))
        self.cx.commit()

    def _author(self, raw_name, raw_email, canon_name, canon_email):
        key = (raw_name, raw_email)
        if key in self.author_cache:
            return self.author_cache[key]
        row = self.cx.execute(
            "SELECT id FROM authors WHERE repo_id = ? AND name = ? AND email = ?",
            (self.repo_id, canon_name, canon_email)).fetchone()
        if row:
            author_id = row[0]
        else:
            author_id = self.cx.execute(
                "INSERT INTO authors (repo_id, name, email) VALUES (?, ?, ?)",
                (self.repo_id, canon_name, canon_email)).lastrowid
        self.cx.execute(
            "INSERT OR REPLACE INTO author_map (repo_id, raw_name, raw_email, author_id)"
            " VALUES (?, ?, ?, ?)", (self.repo_id, raw_name, raw_email, author_id))
        self.author_cache[key] = author_id
        return author_id


def _resolve_path(raw):
    """Resolve numstat rename forms to the new path: 'a => b', 'pre/{a => b}/post'."""
    if " => " not in raw:
        return raw
    if "{" in raw and "}" in raw and raw.index("{") < raw.index("}"):
        pre, rest = raw.split("{", 1)
        inner, post = rest.split("}", 1)
        _, new = inner.split(" => ", 1)
        return pre + new + post
    _, new = raw.split(" => ", 1)
    return new


def _ancestor_dirs(path):
    """All ancestor directories of a file path, including the root ''. """
    dirs = [""]
    acc = ""
    for part in path.split("/")[:-1]:
        acc = f"{acc}/{part}" if acc else part
        dirs.append(acc)
    return dirs


def _load_files(cx, repo_id, git_dir):
    out = subprocess.run(
        ["git", "-C", str(git_dir), "ls-tree", "-r", "--name-only", "-z", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        return
    paths = [p for p in out.stdout.split("\0") if p]
    cx.executemany("INSERT OR IGNORE INTO files (repo_id, path) VALUES (?, ?)",
                   [(repo_id, p) for p in paths])
    cx.commit()
