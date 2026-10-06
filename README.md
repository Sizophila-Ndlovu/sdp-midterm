# Repo Analysis Tool (RAT) — COMS3011A Test

A web dashboard that ingests git repositories (zip upload or clone URL) and reports file /
directory / repository / commit-set / author metrics, filterable by repository, author, object
(file or directory) and commit set (a time period or a manually selected list of commits).

## Features

- **Ingestion** — zip upload (with `.git`) or full bare clone from any git URL, with live
  progress in the UI. Repositories stay listed; multiple repositories are supported side by side.
- **Metrics** — line metrics l+, l−, δ (growth), λ (churn); n (modifications), η (modification
  frequency) and ρ (churn rate); per-author n, λ and ω (ownership) — for files, directories
  (recursive) and the whole repository.
- **Filters** — repository, author, object (tree browser + search) and commit set: all time,
  last 7/30/90 days, custom range [i, j), or a manually selected commit list (searchable,
  paginated picker).
- **Author merging** — `.mailmap` is applied automatically at ingest; a merge dialog re-points
  commits when the same person committed under several names/emails.
- **Visualisation** — churn timeline (auto day/month buckets) and ownership doughnut, plus
  sortable/searchable/paginated tables with CSV export everywhere.
- **Correctness rules from the brief** — only non-merge commits reachable from HEAD; committer
  dates for time filters; rename detection at 50 % (pure renames change nothing, edits count on
  the new path); binary files excluded via git; deletions count as removed lines on their path.

## Quick start

```bash
pip install -r requirements.txt
python app.py            # then open http://localhost:5000
```

Add a repository from the left rail (zip or clone URL) and it is analysed automatically.

## Docs

- [PLAN.md](PLAN.md) — architecture, data model, ingestion + metric design, milestones
- [Priority.md](Priority.md) — rubric-guided work order (what we built first, and why)

## How it works (short version)

Everything expensive happens once, at ingest: a single streaming `git log --numstat -M50%`
pass stores per-commit, per-file line deltas in SQLite (plus a `dir_commits` table for exact
directory modification counts). Every dashboard question afterwards — under any filter — is an
indexed SQL aggregation, so the UI is a thin renderer over one `POST /metrics` call per filter
change. See [PLAN.md](PLAN.md) §3–§9 for the full design.

API (JSON):

```
GET    /api/repos                     list repos + status
POST   /api/repos                     add repo (multipart zip | {"url": ...})
GET    /api/repos/<id>/status         ingest progress
DELETE /api/repos/<id>
GET    /api/repos/<id>/authors        canonical authors + raw aliases
POST   /api/repos/<id>/authors/merge  {"target": id, "sources": [id, ...]}
GET    /api/repos/<id>/commits        paginated/searchable commit list
GET    /api/repos/<id>/tree           object browser (?path=) and search (?q=)
GET|POST /api/repos/<id>/metrics      everything for one filter in one response
```

## Verified against raw `git`

Totals computed by the tool were cross-checked against independent `git log --numstat`
aggregations (non-merge commits from HEAD, renames at 50 %):

| Repository | Non-merge commits | l+ (tool / git) | l− (tool / git) |
|---|---|---|---|
| cJSON | 955 / 955 | 46 377 / 46 377 | 11 211 / 11 211 |
| redis | 11 874 / 11 874 | 1 110 258 / 1 110 258 | 500 312 / 500 312 |
| git | 61 101 / 61 101 | 4 070 371 / 4 070 371 | 2 375 604 / 2 375 604 |

A hand-computed fixture repository (renames, delete, binary file, mailmap alias, merge commit)
is covered by `data/smoke.py` (31 exact-value checks, all passing), and the UI flows were
exercised in a real browser session (no console errors).

**Performance at scale:** ingesting git (~61 000 non-merge commits) takes ≈ 1 minute (clone +
parse); afterwards every dashboard interaction is an indexed query — root view ≈ 0.5 s first
call, filtered/searched views ≈ 0.1–0.3 s, commit search ≈ 0.03 s (SQLite, 34 MB for all four
repositories).

## AI Declaration

AI Declaration: Qoder (agentic coding IDE) — code written with AI assistance; every metric
verified against raw `git` and reviewed before submission.

*(confirm this line before submitting)*
