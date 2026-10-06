"""Metric computations over the filtered commit set H (brief section 2).

All numbers come from two row tables -- `file_changes` (per commit, per file line
deltas) and `dir_commits` (which directories a commit touched) -- aggregated with
indexed SQL. The commit-set filter (author / time range / manual commit list) is
applied to every aggregate, so any filter change recomputes exactly.
"""
DAY = 86400
_SORTS = {
    "churn": "SUM(fc.added + fc.removed)",
    "added": "SUM(fc.added)",
    "removed": "SUM(fc.removed)",
    "growth": "SUM(fc.added) - SUM(fc.removed)",
    "modifications": "COUNT(DISTINCT fc.commit_id)",
    "path": "fc.path",
}
_CACHE = {}
_CACHE_MAX = 256


def invalidate(repo_id=None):
    if repo_id is None:
        _CACHE.clear()
    else:
        for key in [k for k in _CACHE if k[1] == repo_id]:
            del _CACHE[key]


def _cached(key, fn):
    if key in _CACHE:
        return _CACHE[key]
    value = fn()
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = value
    return value


def _h_clause(repo_id, author_id=None, since=None, until=None, commit_ids=None):
    """WHERE fragment for the commit set H (against alias c)."""
    sql = ["c.repo_id = ?"]
    params = [int(repo_id)]
    if author_id:
        sql.append("c.author_id = ?")
        params.append(int(author_id))
    if since is not None:
        sql.append("c.committer_ts >= ?")
        params.append(int(since))
    if until is not None:
        sql.append("c.committer_ts < ?")
        params.append(int(until))
    if commit_ids:
        sql.append("c.id IN (%s)" % ",".join("?" * len(commit_ids)))
        params.extend(int(i) for i in commit_ids)
    return " AND ".join(sql), params


def _range_clause(path):
    """Row predicate for all files strictly below directory `path` ('' = whole repo).

    String range [path/, path0) matches exactly the descendants of `path`, and can use
    the (repo_id, path) index (LIKE would need escaping and scan).
    """
    if not path:
        return "", []
    return "fc.path >= ? AND fc.path < ?", [path + "/", path + "0"]


def _ratio(num, den):
    return (num / den) if den else 0.0


def _like(q):
    q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


def _with_rates(row, h_count):
    row["modification_frequency"] = _ratio(row["modifications"], h_count)
    row["churn_rate"] = _ratio(row["churn"], h_count)
    return row


def count_commits(cx, repo_id, **f):
    where, params = _h_clause(repo_id, **f)
    return cx.execute(f"SELECT COUNT(*) FROM commits c WHERE {where}", params).fetchone()[0]


def object_metrics(cx, repo_id, path, is_dir, **f):
    """l+, l-, delta, lambda, n for a file or directory object ('' = root)."""
    where, params = _h_clause(repo_id, **f)
    if is_dir:
        rc, rp = _range_clause(path)
        cond = f"{where} AND {rc}" if rc else where
        added, removed = cx.execute(
            "SELECT COALESCE(SUM(fc.added), 0), COALESCE(SUM(fc.removed), 0)"
            " FROM file_changes fc JOIN commits c ON c.id = fc.commit_id"
            f" WHERE {cond}", params + rp).fetchone()
        n = cx.execute(
            "SELECT COUNT(*) FROM dir_commits dc JOIN commits c ON c.id = dc.commit_id"
            f" WHERE {where} AND dc.dir = ?", params + [path]).fetchone()[0]
    else:
        added, removed, n = cx.execute(
            "SELECT COALESCE(SUM(fc.added), 0), COALESCE(SUM(fc.removed), 0),"
            " COUNT(DISTINCT fc.commit_id)"
            " FROM file_changes fc JOIN commits c ON c.id = fc.commit_id"
            f" WHERE {where} AND fc.path = ?", params + [path]).fetchone()
    return {"added": added, "removed": removed, "growth": added - removed,
            "churn": added + removed, "modifications": n}


def subtree_rows(cx, repo_id, path, **f):
    """Grouped metrics for every changed file under `path` ('' = whole repo)."""
    where, params = _h_clause(repo_id, **f)
    rc, rp = _range_clause(path)
    cond = f"{where} AND {rc}" if rc else where
    rows = cx.execute(
        "SELECT fc.path AS path, SUM(fc.added) AS added, SUM(fc.removed) AS removed,"
        " COUNT(DISTINCT fc.commit_id) AS modifications"
        " FROM file_changes fc JOIN commits c ON c.id = fc.commit_id"
        f" WHERE {cond} GROUP BY fc.path", params + rp).fetchall()
    return [dict(r) for r in rows]


def files_page(cx, repo_id, path, limit, offset, sort="churn", q=None, **f):
    """Recursive file table under `path`, paginated and sorted (server-side)."""
    where, params = _h_clause(repo_id, **f)
    cond, cparams = where, list(params)
    rc, rp = _range_clause(path)
    if rc:
        cond += f" AND {rc}"
        cparams += rp
    if q:
        cond += " AND fc.path LIKE ? ESCAPE '\\'"
        cparams.append(_like(q))
    order = _SORTS.get(sort, _SORTS["churn"])
    direction = "ASC" if order == "fc.path" else "DESC"
    rows = cx.execute(
        "SELECT fc.path AS path, SUM(fc.added) AS added, SUM(fc.removed) AS removed,"
        " COUNT(DISTINCT fc.commit_id) AS modifications"
        " FROM file_changes fc JOIN commits c ON c.id = fc.commit_id"
        f" WHERE {cond} GROUP BY fc.path ORDER BY {order} {direction} LIMIT ? OFFSET ?",
        cparams + [int(limit), int(offset)]).fetchall()
    total = cx.execute(
        "SELECT COUNT(DISTINCT fc.path) FROM file_changes fc"
        " JOIN commits c ON c.id = fc.commit_id"
        f" WHERE {cond}", cparams).fetchone()[0]
    out = []
    for r in rows:
        d = dict(r)
        d["growth"] = d["added"] - d["removed"]
        d["churn"] = d["added"] + d["removed"]
        out.append(d)
    return {"rows": out, "total": total}


def directory_view(cx, repo_id, path, **f):
    """Immediate children (dirs + files) of `path` with metrics attached."""
    rows = subtree_rows(cx, repo_id, path, **f)
    cut = len(path) + 1 if path else 0
    child_files, seen = [], set()
    rollup = {}
    for r in rows:
        rel = r["path"][cut:]
        parts = rel.split("/")
        if len(parts) == 1:
            child_files.append(r)
            seen.add(r["path"])
        else:
            d = f"{path}/{parts[0]}" if path else parts[0]
            acc = rollup.setdefault(d, {"added": 0, "removed": 0})
            acc["added"] += r["added"]
            acc["removed"] += r["removed"]

    # include children that exist at HEAD but had no changes under this filter
    if path:
        file_rows = cx.execute(
            "SELECT path FROM files WHERE repo_id = ? AND path >= ? AND path < ?",
            (repo_id, path + "/", path + "0")).fetchall()
    else:
        file_rows = cx.execute("SELECT path FROM files WHERE repo_id = ?",
                               (repo_id,)).fetchall()
    for r in file_rows:
        rel = r["path"][cut:]
        if "/" in rel:
            d = f"{path}/{rel.split('/', 1)[0]}" if path else rel.split("/", 1)[0]
            rollup.setdefault(d, {"added": 0, "removed": 0})
        elif r["path"] not in seen:
            child_files.append({"path": r["path"], "added": 0, "removed": 0, "modifications": 0})

    # exact modification counts for the child directories (which no sum of files gives)
    counts = {}
    if rollup:
        where, params = _h_clause(repo_id, **f)
        names = list(rollup)
        for i in range(0, len(names), 400):
            chunk = names[i:i + 400]
            q = ("SELECT dc.dir AS dir, COUNT(*) AS n FROM dir_commits dc"
                 " JOIN commits c ON c.id = dc.commit_id"
                 f" WHERE {where} AND dc.dir IN ({','.join('?' * len(chunk))})"
                 " GROUP BY dc.dir")
            for r in cx.execute(q, params + chunk):
                counts[r["dir"]] = r["n"]

    dirs = []
    for name, acc in rollup.items():
        added, removed = acc["added"], acc["removed"]
        dirs.append({"path": name, "added": added, "removed": removed,
                     "growth": added - removed, "churn": added + removed,
                     "modifications": counts.get(name, 0)})
    dirs.sort(key=lambda d: d["churn"], reverse=True)

    files = []
    for r in child_files:
        files.append({"path": r["path"], "added": r["added"], "removed": r["removed"],
                      "growth": r["added"] - r["removed"],
                      "churn": r["added"] + r["removed"],
                      "modifications": r["modifications"]})
    files.sort(key=lambda d: d["churn"], reverse=True)
    return {"dirs": dirs, "files": files}


def authors_view(cx, repo_id, path, is_dir, **f):
    """n, lambda and ownership omega per author for the selected object."""
    where, params = _h_clause(repo_id, **f)
    base = cx.execute("SELECT id, name, email FROM authors WHERE repo_id = ? ORDER BY name",
                      (repo_id,)).fetchall()
    agg = {}
    if is_dir:
        rc, rp = _range_clause(path)
        cond = f"{where} AND {rc}" if rc else where
        for r in cx.execute(
                "SELECT c.author_id AS aid, SUM(fc.added) AS added, SUM(fc.removed) AS removed"
                " FROM file_changes fc JOIN commits c ON c.id = fc.commit_id"
                f" WHERE {cond} GROUP BY c.author_id", params + rp):
            agg.setdefault(r["aid"], {}).update({"added": r["added"], "removed": r["removed"]})
        for r in cx.execute(
                "SELECT c.author_id AS aid, COUNT(*) AS n FROM dir_commits dc"
                " JOIN commits c ON c.id = dc.commit_id"
                f" WHERE {where} AND dc.dir = ? GROUP BY c.author_id", params + [path]):
            agg.setdefault(r["aid"], {})["n"] = r["n"]
    else:
        for r in cx.execute(
                "SELECT c.author_id AS aid, SUM(fc.added) AS added, SUM(fc.removed) AS removed,"
                " COUNT(DISTINCT fc.commit_id) AS n"
                " FROM file_changes fc JOIN commits c ON c.id = fc.commit_id"
                f" WHERE {where} AND fc.path = ? GROUP BY c.author_id", params + [path]):
            agg[r["aid"]] = {"added": r["added"], "removed": r["removed"], "n": r["n"]}

    total_churn = object_metrics(cx, repo_id, path, is_dir, **f)["churn"]
    out = []
    for b in base:
        a = agg.get(b["id"], {})
        added, removed, n = a.get("added", 0), a.get("removed", 0), a.get("n", 0)
        churn = added + removed
        out.append({"id": b["id"], "name": b["name"], "email": b["email"],
                    "added": added, "removed": removed, "growth": added - removed,
                    "churn": churn, "modifications": n,
                    "ownership": _ratio(churn, total_churn)})
    out.sort(key=lambda r: r["churn"], reverse=True)
    return out


def timeline(cx, repo_id, path, is_dir, **f):
    """Churn over time for the object; day buckets up to ~4 months, else months."""
    where, params = _h_clause(repo_id, **f)
    rc, rp = _range_clause(path)
    cond = f"{where} AND {rc}" if rc else where
    lo, hi = cx.execute(
        f"SELECT MIN(c.committer_ts), MAX(c.committer_ts) FROM commits c WHERE {where}",
        params).fetchone()
    if lo is None:
        return {"bucket": "day", "points": []}
    monthly = (hi - lo) > 120 * DAY
    if monthly:
        rows = cx.execute(
            "SELECT strftime('%Y-%m', c.committer_ts, 'unixepoch') AS bucket,"
            " SUM(fc.added) AS added, SUM(fc.removed) AS removed"
            " FROM file_changes fc JOIN commits c ON c.id = fc.commit_id"
            f" WHERE {cond} GROUP BY bucket ORDER BY bucket", params + rp).fetchall()
    else:
        rows = cx.execute(
            f"SELECT c.committer_ts / {DAY} AS bucket,"
            " SUM(fc.added) AS added, SUM(fc.removed) AS removed"
            " FROM file_changes fc JOIN commits c ON c.id = fc.commit_id"
            f" WHERE {cond} GROUP BY bucket ORDER BY bucket", params + rp).fetchall()
    points = []
    for r in rows:
        t = r["bucket"] if monthly else r["bucket"] * DAY
        points.append({"t": t, "added": r["added"], "removed": r["removed"],
                       "churn": r["added"] + r["removed"]})
    return {"bucket": "month" if monthly else "day", "points": points}


def commits_page(cx, repo_id, q=None, page=1, per_page=50):
    """Paginated commit list for the manual commit-selection filter."""
    per_page = max(1, min(int(per_page), 200))
    page = max(1, int(page))
    cond = "c.repo_id = ?"
    params = [int(repo_id)]
    if q:
        like = _like(q)
        cond += (" AND (c.sha LIKE ? ESCAPE '\\' OR c.summary LIKE ? ESCAPE '\\'"
                 " OR a.name LIKE ? ESCAPE '\\' OR a.email LIKE ? ESCAPE '\\')")
        params += [like] * 4
    rows = cx.execute(
        "SELECT c.id, c.sha, c.committer_ts, c.summary, a.name AS author_name,"
        " a.email AS author_email FROM commits c JOIN authors a ON a.id = c.author_id"
        f" WHERE {cond} ORDER BY c.committer_ts DESC, c.id DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page]).fetchall()
    total = cx.execute(
        "SELECT COUNT(*) FROM commits c JOIN authors a ON a.id = c.author_id"
        f" WHERE {cond}", params).fetchone()[0]
    return {"rows": [dict(r) for r in rows], "total": total, "page": page,
            "per_page": per_page}


def tree_children(cx, repo_id, path=""):
    """Immediate children (dirs + files) for the object picker, from HEAD."""
    if path:
        rows = cx.execute(
            "SELECT path FROM files WHERE repo_id = ? AND path >= ? AND path < ? ORDER BY path",
            (repo_id, path + "/", path + "0")).fetchall()
    else:
        rows = cx.execute("SELECT path FROM files WHERE repo_id = ? ORDER BY path",
                          (repo_id,)).fetchall()
    cut = len(path) + 1 if path else 0
    dirs, files = set(), []
    for r in rows:
        rel = r["path"][cut:]
        if "/" in rel:
            child = rel.split("/", 1)[0]
            dirs.add(f"{path}/{child}" if path else child)
        elif rel:
            files.append(r["path"])
    return {"dirs": sorted(dirs), "files": files}


def objects_search(cx, repo_id, q, limit=60):
    """Search files by path substring; returns matching files and their directories."""
    rows = cx.execute(
        "SELECT path FROM files WHERE repo_id = ? AND path LIKE ? ESCAPE '\\'"
        " ORDER BY path LIMIT ?", (repo_id, _like(q), int(limit))).fetchall()
    files, dirs = [], set()
    for r in rows:
        p = r["path"]
        files.append({"path": p, "is_dir": False})
        if "/" in p:
            dirs.add(p.rsplit("/", 1)[0])
    for d in sorted(dirs):
        files.append({"path": d, "is_dir": True})
    return {"objects": files}


def dashboard(cx, repo_id, path="", is_dir=True, files_limit=200, files_offset=0,
              sort="churn", q=None, **f):
    """Everything the UI needs for one filter in a single response."""
    key = ("dash", int(repo_id), path, bool(is_dir), int(files_limit), int(files_offset),
           sort, q or "", tuple(sorted((k, tuple(v) if isinstance(v, list) else v)
                                       for k, v in f.items())))
    return _cached(key, lambda: _dashboard(cx, repo_id, path, is_dir, files_limit,
                                           files_offset, sort, q, f))


def _dashboard(cx, repo_id, path, is_dir, files_limit, files_offset, sort, q, f):
    h_count = count_commits(cx, repo_id, **f)
    obj = object_metrics(cx, repo_id, path, is_dir, **f)
    obj["modification_frequency"] = _ratio(obj["modifications"], h_count)
    obj["churn_rate"] = _ratio(obj["churn"], h_count)

    files = files_page(cx, repo_id, path, files_limit, files_offset, sort, q=q, **f)
    for row in files["rows"]:
        _with_rates(row, h_count)

    dirs = None
    if is_dir:
        view = directory_view(cx, repo_id, path, **f)
        for row in view["dirs"] + view["files"]:
            _with_rates(row, h_count)
        dirs = view

    return {
        "h_count": h_count,
        "object": obj,
        "files": files,
        "dirs": dirs,
        "authors": authors_view(cx, repo_id, path, is_dir, **f),
        "timeline": timeline(cx, repo_id, path, is_dir, **f),
    }
