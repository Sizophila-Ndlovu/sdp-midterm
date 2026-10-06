# PLAN.md — Repo Analysis Tool (RAT)

**Task:** COMS3011A test — build a Repo Analysis Tool (RAT): a web dashboard that ingests git
repositories and reports the file / directory / repository / commit-set / author metrics defined
in the brief.

**Submission:** this public repository.

**Companion:** [Priority.md](Priority.md) — rubric-guided build order. When in doubt about what
to do next, that file wins.

---

## 1. Guiding principle

**Simplest architecture that is still correct and fast.** Chosen deliberately: the 50%
requirements grade rides on metric correctness, not on infrastructure.

- One small Python service (Flask) serving both the JSON API and the static dashboard.
- One SQLite database as the only state.
- No build step, no frontend framework, no ORM, no queue.
- All git access through the `git` CLI (subprocess), which gives the exact diff semantics the
  brief demands (rename detection, binary detection, mailmap) with the least code.

Everything expensive happens **once, at ingest**: a single pass over history stores per-commit,
per-file line deltas. Every question the dashboard asks afterwards (under any filter) is an
indexed SQL aggregation. That is the whole trick.

## 2. Stack

| Layer | Choice | Why |
|---|---|---|
| Runtime | Python 3.12 | already available; best fit for this parsing work |
| Web | Flask | JSON API + static file hosting in minimal code |
| Git | `git` CLI via `subprocess` | exact brief semantics (`-M50%` renames, binary `-`, mailmap); far faster and simpler than GitPython at ~100k commits |
| Storage | SQLite (WAL mode) | indexed aggregation over millions of rows; zero setup; one file per install |
| Frontend | Static HTML/CSS/JS + Chart.js (vendored locally) | no build step; works offline during the demo |
| Tests | pytest | regression tests for metric math |

Runtime dependency: `flask` only. Dev dependency: `pytest`. (`requirements.txt` added in M0.)

## 3. Architecture

```
Browser (static dashboard, Chart.js)
   |  JSON over HTTP
   v
Flask app (app.py)  ----  /api/*  ----  static/
   |                          |
   |                          v
   |                    SQLite (repos, authors, author_map, commits, file_changes, files)
   |                          ^
   v                          |
worker thread (ingest.py): zip unzip / git clone -> single `git log` stream -> batched inserts
```

Flat layout (deliberately simple):

| Path | Responsibility |
|---|---|
| `app.py` | Flask app, routes, static hosting, error handling |
| `ingest.py` | zip extraction, deep clone, `git log` streaming parser, batched DB writes |
| `metrics.py` | all metric queries (commit set + object + author) and directory rollup |
| `authors.py` | mailmap handling, manual author merging |
| `storage.py` | SQLite schema + connection helpers |
| `static/` | `index.html`, `app.js`, `style.css`, `vendor/chart.umd.js` |
| `tests/` | fixture git repos with known expected metrics |

## 4. Data model (SQLite)

```sql
repos(id, name, source_type[zip|url], source, git_dir, status[ingesting|ready|error],
      progress REAL, message, head_sha, commit_count, created_at)

authors(id, repo_id, name, email)                    -- canonical authors
author_map(repo_id, raw_name, raw_email, author_id)  -- raw pair -> canonical (identity entries
                                                     --   + manual merges)

commits(id, repo_id, sha, raw_name, raw_email, author_id, committer_ts, summary)

file_changes(repo_id, commit_id, path, added, removed)
    -- one row per (commit, file) with a REAL line change (added+removed > 0);
    -- binary files never stored; renames stored under the NEW path

files(repo_id, path)                                 -- files present at HEAD, so objects with
                                                     -- zero metrics can still be listed
```

Indexes: `commits(repo_id, committer_ts)`, `commits(repo_id, author_id)`,
`file_changes(commit_id)`, `file_changes(repo_id, path)`.

Notes:

- Zero-change rows are never stored, so `added`/`removed` and "was modified in h" are the same
  fact → `n` (modifications) is `COUNT(DISTINCT commit_id)`.
- Directory and repository metrics are **not materialized**: they are prefix sums over file
  rows (EXPLAIN-friendly with the `(repo_id, path)` index). Storing per-directory rows would
  multiply ingest writes for no query speed gain we need.
- Per-commit metrics (brief §2.1) are simply the `H = {h}` case — reachable through the manual
  commit-list filter with a single commit selected.

## 5. Ingestion pipeline

1. **Zip upload** — safe extraction (reject absolute paths / `..` members) to `data/repos/<id>/`.
   Locate the repo root (`.git` at top level or one folder down; `.git` may be a directory or a
   gitdir-pointer file). Missing `.git` → clear user-facing error.
2. **Clone URL** — `git clone --bare <url> data/repos/<id>.git` (deep clone, full history, no
   working tree needed).
3. Progress denominator: `git rev-list --no-merges --count HEAD`.
4. **Single streaming pass** (the core of the tool):
   `git log --no-merges --numstat -M50% -z --use-mailmap --format=<NUL-delimited header>`
   - only non-merge commits reachable from HEAD (H̄), as the brief defines;
   - `--numstat` rows give `added \t removed \t path` per changed file;
   - `-` rows are binary → skipped;
   - rename forms (`old => new`, `dir/{a => b}.c`) are resolved to the **new path**; a pure
     rename emits 0/0 and is skipped, edits land on the new path (brief rule);
   - deletions arrive naturally as `0 \t N \t path` and count as removed lines on that path;
   - the root commit reports the full file contents as additions (h[p] = empty commit);
   - headers: sha, parent sha, `%an/%ae` (raw), `%aN/%aE` (canonical), `%ct` (committer date),
     subject.
5. Batch inserts (one transaction per ~5k commits) into `commits` + `file_changes`; `progress`
   is updated continuously so the UI can poll `/api/repos/<id>/status`.
6. **Authors** — raw pair kept on every commit; canonical pair (`%aN/%aE`, git applies the
   repository's `.mailmap`) becomes an `authors` row; `author_map` records raw → canonical.
   A fixture test pins git's mailmap behaviour; fallback if it misbehaves: parse `.mailmap` in
   Python. Manual merges rewrite `author_map` + `commits.author_id` for the affected raw pairs.

## 6. Metric engine (brief §2 → queries)

The commit set `H` is the single input to every metric; it is always expressed as one SQL
condition on `commits`, never precomputed:

```sql
-- H =
SELECT id FROM commits
WHERE repo_id = :repo
  [AND author_id = :author]                  -- author filter
  [AND committer_ts >= :i]                   -- time period: H_t (t to now) or H_i,j ([i, j))
  [AND committer_ts <  :j]
  [AND id IN (:commit_list)]                 -- manual commit selection
```

Aggregates (object `o` = file path, or directory prefix, or root):

| Metric | Computation |
|---|---|
| l⁺h,f, l⁻h,f | `added`, `removed` columns of `file_changes` |
| δh,f = l⁺−l⁻, λh,f = l⁺+l⁻ | derived per row |
| l⁺H,o, l⁻H,o, δH,o, λH,o | SUM over H-filtered rows whose path matches o |
| nH,o (modifications) | COUNT(DISTINCT commit_id) — every stored row is a modification |
| ηH,o, ρH,o | `n/|H|`, `λ/|H|`; `|H|` = COUNT of commits in the H query |
| nH,o,a, λH,o,a | the same aggregates filtered to `c.author_id = a` |
| ωH,o,a (ownership) | `λH,o,a / λH,o`, summed safely (0 when λH,o = 0) |
| directory metrics | prefix `path = o OR path LIKE o||'/%'` — the brief's recursive sum reduces to exactly this |
| repository metrics | root — no path filter |
| subtree views (UI) | pull grouped file rows once, roll ancestors up to directories in Python |

Correctness rules encoded from the brief:

- only non-merge commits reachable from HEAD participate (`--no-merges`);
- all time filters use the **committer date**;
- rename detection at 50%: pure renames don't change metrics; edit+rename attributes to the new path;
- deleted objects keep their removed-lines change on their (old) path;
- binary files are excluded via git's own detection;
- changing any filter changes H exactly, so all metrics are always consistent with the filter.

## 7. API surface

```
GET    /api/repos                     list repos + status/progress
POST   /api/repos                     add repo: multipart zip OR {"url": "..."}
GET    /api/repos/<id>/status         ingest progress (polled while ingesting)
DELETE /api/repos/<id>                remove repo + its data
GET    /api/repos/<id>/authors        canonical authors + raw aliases
POST   /api/repos/<id>/authors/merge  {"target": <id>, "sources": [<id>, ...]}
GET    /api/repos/<id>/commits        paginated commits for manual selection (search by sha/author/date)
GET    /api/repos/<id>/tree           file/dir listing for the object picker
GET    /api/repos/<id>/metrics        one call per filter change; query params:
        author, object (path or ''), since, until, commits (ids or csv of ids | all)
        returns: summary totals, |H|, timeline buckets, file rows, directory rollup,
                 author breakdown (n, λ, ω)
```

One metrics endpoint → one round trip per filter change → the UI stays dumb, all math stays
server-side, and responses are memoized by (repo, filter signature).

## 8. Dashboard

Layout: left rail (repos, add-repo, ingest progress) · top filter bar · main panels.

- **Filter bar:** repository ▸ author ▸ object (tree/search picker) ▸ commit set
  (All time / last 7-30-90 days / custom range [i, j) / manual commit list with search+select).
- **Panels:**
  1. Summary cards — |H|, author count, Σ added / removed / growth / churn.
  2. Churn timeline — area/bar chart, auto-bucketed (day → week → month by span).
  3. Files table — path + l⁺, l⁻, δ, λ, n, η, ρ (sortable, searchable, server-paginated).
  4. Directory view — click to roll up into a directory; breadcrumbs back to root; child dirs
     and files with the same columns.
  5. Authors — n, λ, ω table for the selected object + ownership chart (stacked bar/doughnut).
  6. Churn treemap — directories sized by churn (Chart.js treemap plugin).
- **QoL:** CSV export of any table, sortable columns, loading skeletons, friendly empty states,
  repo delete, filters preserved while navigating.

## 9. Performance (target: ~100,000 commits, e.g. git.git)

- Ingest is the only expensive step: one `git log` pass + batched inserts in a worker thread
  with live progress (deep clone is network-bound; parsing is diff-bound). Expected: seconds for
  cJSON, ~1 min for redis, a few minutes for git.git — once, then cached in SQLite.
- After ingest, every dashboard action is an indexed aggregate query (ms to low-100s of ms).
- Subtree panels: one grouped query + Python ancestor rollup; memoized per filter signature.
- Tables paginate server-side; the browser never sees more than a page of rows.
- Stretch (only if M4 has slack): parallelize ingest by splitting history into disjoint
  `git log` ranges across a few processes.

## 10. Verification

- pytest fixture repos covering: rename with edit, pure rename, deletion, binary file, merge
  commit (must be ignored), two authors, mailmap merge. Assert exact metric values.
- Invariant tests: root δ = Σ file δ; growth = added − removed; Σ authors' λ = total λ;
  Σ authors' ω = 1 (when λ > 0); manual commit filter of one commit = that commit's metrics.
- Manual cross-check against the provided sample metrics at their commit hashes
  (cJSON first, then redis, then git).

## 11. Milestones (2.5h test budget)

| # | Time | Deliverable |
|---|---|---|
| M0 | 15m | scaffold, schema, zip ingestion walking skeleton (P0.1-P0.2) |
| M1 | 45m | clone ingestion, full parser, all metric queries correct (P0.3-P0.5) |
| M2 | 30m | filtering + author merging + multi-repo (P1) |
| M3 | 30m | dashboard UI + charts (P2) |
| M4 | 20m | error handling, CSV/QoL, perf pass (P3) |
| M5 | 10m | verification vs sample metrics, README, final push |

## 12. Git workflow

- Trunk-based on `main`; commit at each milestone (or smaller) with clear messages; push often.
- `data/` (cloned repos, SQLite db) stays gitignored — never commit cloned repositories.

## 13. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Rename-limit warnings on pathological commits | accept git's auto-scaled limit; metric error is tiny and documented in README |
| Zip with `.git` pointing to a missing gitdir | detect during locate-root step → clear error ("re-export the repo with history") |
| Huge numstat streams | stream-parse line by line (never buffer whole output); batched inserts |
| Long ingest blocks the UI | worker thread + progress polling; dashboard usable while ingesting |
| Push authentication | settle git credentials before the final submission push |
| Time overrun | [Priority.md](Priority.md) order is the tie-breaker: metric correctness > everything |
