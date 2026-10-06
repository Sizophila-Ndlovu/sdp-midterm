# Priority.md — rubric-guided work order

Read this before every work session: **do the top unchecked item next.**

## Why this order (facts from the rubric)

- Weights: **Requirements 50%**, Architectural & UI Design 25%, Usability 25%.
- Requirement tiers are **cumulative** — a tier is only reachable if every previous tier is
  satisfied, and each tier is judged holistically:
  - ≤25%: *some* metric categories correct; *either* zip or URL ingestion.
  - ≤50%: **all metrics correct, both zip and URL ingestion.**
  - ≤75%: + filtering, author merge, multi-repo support (the rubric says "either", we build all
    three — each is small on this architecture).
  - ≤100%: + filtering, author merge AND multi-repo support (all three).
- Conclusion: **wrong or missing metrics cap the whole submission regardless of UI quality.**
  Metrics first, always.

## P0 — Get to the 50% requirement tier (all metrics correct, both ingestions)

- [ ] P0.1 Storage schema + streaming `git log --numstat -M50%` parser
      (non-merge commits only, committer dates, renames to new path, binary skipped, deletions counted)
- [ ] P0.2 Zip ingestion (repo with `.git` dir or pointer file; safe extraction)
- [ ] P0.3 Clone-URL ingestion (deep clone)
- [ ] P0.4 File metrics (per commit) + directory metrics + repository metrics (root)
- [ ] P0.5 Commit-set metrics (l⁺, l⁻, δ, λ, n, η, ρ) + author metrics (n·a, λ·a, ownership ω)
- [ ] P0.6 Verify against the provided sample metrics (cJSON first, then redis, then git)

## P1 — Reach the 75-100% requirements tier (all three features)

- [ ] P1.1 Filtering: repository, author, file/dir object, commit sets
      (time period H_i,j / H_t, and manually selected commit list)
- [ ] P1.2 Author merging: `.mailmap` honoured automatically + manual merge (API + UI)
- [ ] P1.3 Multi-repo support: add / switch / delete repos with isolated data

## P2 — Architectural & UI design (25%)

- [ ] P2.1 Efficient metric computation: single-pass ingest, indexed SQL, memoized aggregate
      responses, no per-request history walking
- [ ] P2.2 Server-side pagination/sorting so large repos stay responsive
- [ ] P2.3 Good -> inspired visualisation: summary cards, churn timeline, files/dirs tables,
      author ownership breakdown, churn treemap, directory drill-down with breadcrumbs

## P3 — Usability (25%)

- [ ] P3.1 Navigation: obvious filter bar, drill-down + breadcrumbs, ingest progress visible
- [ ] P3.2 Error handling: bad zip / missing `.git`, bad URL, unclonable repo, empty filter
      results, ingest failures — friendly, actionable messages everywhere
- [ ] P3.3 Performance: usable end-to-end on a ~100,000-commit repo (background ingest +
      polling, fast aggregate queries, cached)
- [ ] P3.4 QoL: CSV export, search, sortable columns, time-range presets, repo delete,
      empty-state hints

## Tie-breaker rule under time pressure

Drop, in this order: visual polish -> QoL extras -> advanced filter combinations.
Never drop: metric correctness, both ingestion paths, basic filtering + author merge +
multi-repo support.

## Status log (single source of truth — update as we go)

- [ ] P0 complete: metrics verified against at least the cJSON sample
- [ ] P1 complete: all three requirement-tier features demoable
- [ ] P2 complete: queries fast on git.git; visualisation complete
- [ ] P3 complete: error paths manually exercised once
