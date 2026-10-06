"""Flask application: JSON API + static dashboard."""
import logging
import os
import tempfile
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import authors
import ingest
import metrics
import storage

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
storage.init_db()

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3  # 2 GB zip cap


@app.errorhandler(HTTPException)
def _http_error(exc):
    if request.path.startswith("/api"):
        return jsonify({"error": exc.description}), exc.code
    return exc


@app.errorhandler(Exception)
def _server_error(exc):
    app.logger.exception("unhandled error")
    if request.path.startswith("/api"):
        return jsonify({"error": "internal error"}), 500
    raise exc


def _require_ready(cx, repo_id):
    repo = storage.get_repo(cx, repo_id)
    if not repo:
        abort(404, description="no such repository")
    if repo["status"] == "error":
        abort(409, description=repo["message"] or "ingestion failed")
    if repo["status"] != "ready":
        abort(409, description="repository is still ingesting")
    return repo


def _parse_filter(data):
    f = {}
    try:
        author = str(data.get("author") or "").strip()
        if author and author != "all":
            f["author_id"] = int(author)
        for key in ("since", "until"):
            value = data.get(key)
            if value not in (None, "", "all"):
                f[key] = int(value)
        commits = data.get("commits")
        if commits and commits != "all":
            ids = [int(x) for x in str(commits).split(",") if x.strip()]
            f["commit_ids"] = ids or [-1]  # an empty selection matches nothing
    except (TypeError, ValueError):
        abort(400, description="author, since, until and commits must be integers")
    return f


def _valid_url(url):
    return url.startswith(("http://", "https://", "ssh://", "git://")) or \
        (":" in url and "@" in url)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/repos")
def list_repos():
    with storage.db() as cx:
        return jsonify(storage.list_repos(cx))


@app.post("/api/repos")
def add_repo():
    if "file" in request.files:
        upload = request.files["file"]
        if not upload.filename:
            abort(400, description="no file provided")
        tmp_dir = storage.DATA_DIR / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, suffix=".zip")
        os.close(fd)
        upload.save(tmp_path)
        repo_id = ingest.start_zip(tmp_path, Path(upload.filename).name)
        return jsonify({"id": repo_id}), 201

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        abort(400, description="provide a zip file (multipart 'file') "
                               "or a repository URL (json {'url': ...})")
    if not _valid_url(url):
        abort(400, description="that does not look like a git URL "
                               "(expected http(s)://, ssh://, git:// or git@host:path)")
    return jsonify({"id": ingest.start_url(url)}), 201


@app.get("/api/repos/<int:rid>/status")
def repo_status(rid):
    with storage.db() as cx:
        repo = storage.get_repo(cx, rid)
        if not repo:
            abort(404, description="no such repository")
    return jsonify({k: repo[k] for k in ("id", "name", "status", "progress", "message",
                                         "commit_count", "has_mailmap")})


@app.delete("/api/repos/<int:rid>")
def delete_repo(rid):
    with storage.db() as cx:
        repo = storage.get_repo(cx, rid)
        if not repo:
            abort(404, description="no such repository")
        if repo["status"] == "ingesting":
            abort(409, description="repository is still ingesting - wait for it to finish")
        storage.delete_repo(cx, rid)
    ingest.remove_files(rid)
    metrics.invalidate(rid)
    return jsonify({"deleted": rid})


@app.get("/api/repos/<int:rid>/authors")
def repo_authors(rid):
    with storage.db() as cx:
        repo = _require_ready(cx, rid)
        out = authors.list_authors(cx, rid)
    return jsonify({"authors": out, "has_mailmap": bool(repo["has_mailmap"])})


@app.post("/api/repos/<int:rid>/authors/merge")
def merge_authors(rid):
    data = request.get_json(silent=True) or {}
    target, sources = data.get("target"), data.get("sources") or []
    if not target or not sources:
        abort(400, description="provide 'target' and a non-empty 'sources' list")
    with storage.db() as cx:
        _require_ready(cx, rid)
        try:
            authors.merge_authors(cx, rid, target, sources)
        except ValueError as exc:
            abort(400, description=str(exc))
    metrics.invalidate(rid)
    return jsonify({"ok": True})


@app.get("/api/repos/<int:rid>/commits")
def repo_commits(rid):
    with storage.db() as cx:
        _require_ready(cx, rid)
        out = metrics.commits_page(cx, rid, q=request.args.get("q"),
                                   page=request.args.get("page", 1),
                                   per_page=request.args.get("per_page", 50))
    return jsonify(out)


@app.get("/api/repos/<int:rid>/tree")
def repo_tree(rid):
    path = request.args.get("path", "")
    q = request.args.get("q", "").strip()
    with storage.db() as cx:
        _require_ready(cx, rid)
        out = metrics.objects_search(cx, rid, q) if q else metrics.tree_children(cx, rid, path)
    return jsonify(out)


@app.route("/api/repos/<int:rid>/metrics", methods=["GET", "POST"])
def repo_metrics(rid):
    data = (request.get_json(silent=True) or {}) if request.method == "POST" \
        else request.args.to_dict()
    f = _parse_filter(data)
    path = (data.get("object") or "").strip("/")
    is_dir = (data.get("type") or "dir") == "dir"
    if not is_dir and not path:
        abort(400, description="a file object needs a non-empty path")
    try:
        limit = int(data.get("files_limit", 200))
        offset = int(data.get("files_offset", 0))
    except (TypeError, ValueError):
        abort(400, description="files_limit and files_offset must be integers")
    sort = data.get("sort") or "churn"
    with storage.db() as cx:
        _require_ready(cx, rid)
        out = metrics.dashboard(cx, rid, path=path, is_dir=is_dir, files_limit=limit,
                                files_offset=offset, sort=sort, **f)
    return jsonify(out)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)),
            debug=False, threaded=True)
