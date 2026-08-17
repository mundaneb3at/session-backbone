# session-backbone

![CI](https://github.com/mundaneb3at/session-backbone/actions/workflows/ci.yml/badge.svg)

A small, deterministic, **standard-library-only** Python toolchain that turns AI-assistant
session history (raw JSONL transcripts, close/handoff records, chat-app exports) into
auditable ledgers. It exists because session archives rot quietly: claims of "done" get
restated without evidence, counters drift from the data, and partial writes corrupt
downstream reports.

The contract, which every tool here enforces rather than documents:

1. **Every line gets a receipt.** Each physical input line receives exactly one disposition
   receipt — emitted, suppressed, rejected, dropped, or contributed — with a reason code and
   a source locator pointing back at the exact file and line.
2. **Counters recompute.** Every published counter is derivable from the events + receipts it
   summarizes. The test suite recomputes them independently and fails on any drift.
3. **Publication is atomic.** Canonical conversion writes its output bundle
   (`occurrences.jsonl`, `events.jsonl`, `receipts.jsonl`, `counters.json`, hash-bound
   `summary.json`) to a directory that must not already exist — completely, or not at all.
   If the input bytes drift before publication, the run fails with no partial bundle.
4. **Nothing is invented.** Provenance-only status labels (`claimed-done@<session>`, never a
   bare "done"), no fabricated timestamps, no semantic fields synthesized from thin inputs.

Python 3.8+, no dependencies, no build step.

**Windows only.** That is the only platform CI covers, and the only one this has been run
on. `census.py` shells out to `robocopy` (falling back to `scandir` when absent) and to
`git` (tolerated when absent); there are no other platform-specific calls, so Linux and
macOS may well work — but they are untested, and until CI covers them treat them as
unsupported rather than trusting that sentence.

## Quickstart

Run from the repository root. Everything below uses only the committed `fixtures/` — no real
data is required or included.

    python census.py --self-test
    python threads-build.py --self-test
    python threads-query.py --self-test
    python prompt-cluster.py --self-test
    python export-convert.py --self-test
    python map-history.py --self-test
    python -m unittest discover -s tests -p "test_*.py"

Each self-test exits 0, ends with `SELF-TEST PASS: N assertions`, and touches only
`fixtures/`. A captured reference run is committed as `SELF-TEST.txt`. CI runs all seven
commands on every push.

Fixture-driven example invocations (safe to run; they write into `fixtures/_selftest/`):

    python census.py --root "fixtures/census-tree" --out "fixtures/_selftest/census-failure" --exclude "excluded-cache" --dispositions "fixtures/dispositions.csv" --assert-complete
    python threads-build.py --catalog "fixtures/sessions.jsonl" --chests "fixtures/chests" --prompt-estate "fixtures/prompt-estate" --out "fixtures/_selftest/threads/THREADS.md" --json "fixtures/_selftest/threads/threads.jsonl" --now "2026-07-17T12:00:00-07:00"
    python threads-query.py --threads "fixtures/_selftest/threads/threads.jsonl" --query "consolidate storage" --top 5
    python prompt-cluster.py --dir "fixtures/prompt-estate" --out "fixtures/_selftest/cluster/clusters.md" --min-cluster 3
    python export-convert.py --export "fixtures/conversations.json" --out "fixtures/_selftest/export" --min-date 2026-07-01
    python map-history.py --catalog "fixtures/map-history/catalog.jsonl" --prompts "fixtures/map-history/prompts.jsonl" --quotes "fixtures/map-history/quotes.jsonl" --names "fixtures/map-history/names.json" --seeds "fixtures/map-history/seeds.json" --memory-dir "fixtures/map-history/memory" --legacy-close "fixtures/map-history/legacy-close" --session-notes "fixtures/map-history/session-notes" --out "fixtures/_selftest/threads/MAP-HISTORY.md" --require-pre-july

(The census fixture command intentionally exits 1: it demonstrates the `--assert-complete`
gate catching one unassigned file, `lonely.txt`.)

## The tools

| tool | one line |
|---|---|
| `census.py` | Full file-tree census with a disposition ledger (keep/move/archive/junk/gated), git-tracking flags, a robocopy cross-check, and an `--assert-complete` gate that fails on any unassigned path |
| `export-convert.py` | Chat-app export → per-conversation prompt markdown (legacy mode, explicitly lossy), plus the canonical modes that implement the receipt/atomic-publish contract over raw JSONL or prebuilt occurrences |
| `threads-build.py` | Builds a THREADS ledger from a session catalog + topic files + prompt estate. Status is provenance only — a claim is always attributed to the session that made it, and conflicting claims are shown as conflicts, both sides |
| `threads-query.py` | Ranked query over the threads JSONL (token coverage, then recency), with an explicit fallback line when nothing matches |
| `prompt-cluster.py` | Greedy token-set clustering of repeated instructions across sessions (O(n²), documented ceiling ~10,000 prompts), reporting verbatim samples with session provenance |
| `map-history.py` | Recurrence ledger across heterogeneous history lanes (prompts, quotes, close rows, legacy handoffs, session notes, canonical events) with noise stripping, resume-echo collapse, evidence grading, and a citation checker that flags transposed or superseded references |
| `owner_events.py` | Shared canonical-event model: the dependency-free schema executor and runtime validator behind the contract |

## Input contracts (the load-bearing details)

All text I/O is explicit UTF-8 (BOM tolerated where documented). Malformed rows are counted
and skipped, never silently dropped — the counts appear in the `SUMMARY:` JSON line every
tool prints last.

### census.py

- `--root` is the tree; the root itself is emitted as path `.`. `--exclude` matches an entry
  name at any depth; excluded directories record `child_count=N` and are not descended into.
- `--dispositions` is a CSV of `pattern,disposition,reason`. Exact file matches override
  prefixes; otherwise longest prefix wins. Valid dispositions: `keep-in-place`, `move`,
  `archive`, `junk-disposal`, `gated`, `excluded-by-policy` (move/archive require
  `dest=<path>` in reason).
- Outputs `census.csv`, `warnings.csv`, `census-summary.md`; stdout ends with SUMMARY JSON.
  `--assert-complete` exits 1 on any unassigned row after printing up to 50 of them.
- `git ls-files` runs once for tracked flags; the robocopy cross-check (Windows) reports
  count mismatches as warnings, and exclusion asymmetry is explicitly not treated as loss.

### export-convert.py

- Legacy mode (`--export conversations.json --out DIR [--min-date]`) accepts a conversations
  array or an object containing one, extracts human text only, and stays **explicitly
  lossy** — a chat export does not carry enough durable IDs to fabricate canonical events
  truthfully, so this mode never pretends to.
- Canonical raw mode (`--raw-jsonl … --stream-id … --source-label … --canonical-out NEW-DIR`)
  wraps every physical line — blank, malformed, non-object included — into occurrence
  identity + physical provenance. Semantic fields are never invented.
- Canonical prebuilt mode (`--occurrences-jsonl … --canonical-out NEW-DIR`) accepts the
  generic `stream_id` occurrence contract.
- The output directory must not exist; publication is atomic and hash-bound. Input is
  captured once as bytes; observed drift before publication fails with no output bundle.
- Schemas live in `schemas/`. Every ProductionEvent has exactly 14 keys; every physical
  occurrence has exactly one disposition receipt; counters reproduce from events + receipts.
  The schema's `x-uniqueBy` / `x-equalCardinality` / `x-derivationCardinality` clauses are
  normative and enforced by both the schema executor and the runtime validator.

### threads-build.py

- Catalog is tolerant JSONL; rows without a slug are counted and skipped. A goals list uses
  per-goal verdict handling; otherwise a non-empty `status_claimed` emits one
  `claimed-unknown@slug` row (160-char cap). `open_threads > 0` emits an open-threads row.
- Topic membership comes from the registry JSON, never from topic-file prose (a prose
  name-drop must not tag a session — the fixtures include a decoy proving it).
- Similar met/open goals from different sessions (token overlap ≥ 0.6) mark both rows
  `conflict` and report both sources. A bare `done` is never emitted.
- `--now` is required and copied verbatim; the system clock is never substituted.
- Plan pointers that resolve to an ephemeral plans directory are re-pointed to a durable
  archive copy (a live-but-purgeable file is archived at build time, not merely referenced).
  Defaults: plans root `~/.claude/plans`, archive `~/.claude/plans-archive`; override with
  `THREADS_PLANS_ROOT` / `THREADS_PLANS_ARCHIVE`.

### threads-query.py / prompt-cluster.py

- Query ranking: query-token coverage, then ISO-date recency, then normalized goal, then
  slug. No hit prints `NO-MATCH`; a fallback search command is always printed last.
- Clustering normalizes by lowercasing and stripping paths, numbers, punctuation; greedy
  token-set Jaccard at 0.55. Clusters rank by size × mean token count and report three
  verbatim samples with session IDs. The report ends with the unclustered percentage —
  the honest remainder, not a hidden one.

### map-history.py

- Lanes: close rows (outcome authority), pre-cutover prompt/quote corpora, legacy close
  handoffs, session notes, and strict canonical events (`--canonical-events`; invalid or
  duplicate rows fail before any report output).
- Noise stripping removes harness blocks and screenshot/system fragments while preserving
  genuine text that follows them; byte-identical resume echoes under different session IDs
  collapse; `%TEMP%`-cwd sessions drop by default (`--all-cwd` keeps them).
- Recurrence is measured in distinct **days**, not raw record count; same-day fan-out is
  flagged as such. Every row's root-cause citation is verified against `--memory-dir`, and
  transposed (`[!]`), superseded (`[S]`), and missing (`[?]`) citations are flagged.
- Output is byte-deterministic for identical inputs (`--now` is caller-supplied).

## Repository layout

    fixtures/            synthetic inputs used by every self-test (no real data)
    fixtures/_selftest/  reference outputs + scratch dirs the fixture commands write into
                         (threads/ outputs are gitignored: they embed local absolute paths)
    schemas/             the four canonical JSON schemas (occurrence, event, receipt, counters)
    tests/               unittest suite + frozen capture-integrity fixtures (hash-pinned
                         under tests/fixtures/capture_integrity/AUTHORITY.json)

## Verified self-test assertion counts

census 18 · threads-build 27 · threads-query 10 · prompt-cluster 16 · export-convert 15 ·
map-history 58 · unittest suite: 14 tests.

## License

MIT — see [LICENSE](./LICENSE).
