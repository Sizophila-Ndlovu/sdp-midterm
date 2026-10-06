"""Author listing and manual author merging.

The repository's .mailmap is applied by git itself at ingest time (raw pairs are kept,
canonical pairs become author rows). Manual merges simply re-point commits and the
author_map at one canonical author.
"""


def list_authors(cx, repo_id):
    rows = cx.execute(
        "SELECT a.id, a.name, a.email, COUNT(c.id) AS commits"
        " FROM authors a LEFT JOIN commits c ON c.author_id = a.id"
        " WHERE a.repo_id = ? GROUP BY a.id ORDER BY commits DESC, a.name",
        (repo_id,)).fetchall()

    aliases = {}
    for r in cx.execute(
            "SELECT raw_name, raw_email, author_id FROM author_map WHERE repo_id = ?",
            (repo_id,)):
        aliases.setdefault(r["author_id"], []).append(f'{r["raw_name"]} <{r["raw_email"]}>')

    out = []
    for row in rows:
        d = dict(row)
        own = f'{d["name"]} <{d["email"]}>'
        d["merged_from"] = sorted({a for a in aliases.get(d["id"], []) if a != own})
        out.append(d)
    return out


def merge_authors(cx, repo_id, target_id, source_ids):
    """Re-point every commit (and raw alias) of the source authors at the target."""
    sources = [s for s in {int(s) for s in source_ids} if s != int(target_id)]
    if not sources:
        raise ValueError("no source authors given (target excluded from sources)")
    target_id = int(target_id)

    ids = [target_id] + sources
    marks = ",".join("?" * len(ids))
    found = cx.execute(
        f"SELECT COUNT(*) FROM authors WHERE repo_id = ? AND id IN ({marks})",
        [repo_id] + ids).fetchone()[0]
    if found != len(ids):
        raise ValueError("one or more authors do not belong to this repository")

    src_marks = ",".join("?" * len(sources))
    cx.execute(
        f"UPDATE commits SET author_id = ? WHERE repo_id = ? AND author_id IN ({src_marks})",
        [target_id, repo_id] + sources)
    cx.execute(
        f"UPDATE author_map SET author_id = ? WHERE repo_id = ? AND author_id IN ({src_marks})",
        [target_id, repo_id] + sources)
    cx.execute(
        f"DELETE FROM authors WHERE repo_id = ? AND id IN ({src_marks})",
        [repo_id] + sources)
