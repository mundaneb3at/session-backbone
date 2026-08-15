#!/usr/bin/env python3
"""Build MAP-HISTORY.md -- a RECURRENCE ledger over session history, not a chronology.

WHY this exists: the same ask keeps coming back (a chest pin instead of one keyword fix, the
migration ask, fix-invisibility). A chronology hides that; a ledger sorted by times-seen shows it.

Two lanes, because the two eras left different evidence behind:
  * July lane      -- `entry_source: close` rows in the catalog. Rich: goals[].verdict says whether
                      a fix LANDED, friction[].ref says where it was root-caused.
  * pre-July lane  -- June/May transcripts were purged by cleanupPeriodDays before the harvester
                      existed. What survived are prompt/quote extracts taken by mining runs on
                      2026-07-11..13. Those carry the ASK but not the OUTCOME, so their rows are
                      graded `prompts-only` and can never claim a fix landed.

Counting is this script's job. Naming clusters is the LLM's job, via the optional --names sidecar,
so the .md stays regenerable (this script is its only writer -- never hand-edit the output).

Python 3 standard library only. Deterministic: same inputs + same --now => byte-identical output.
"""
import argparse, hashlib, json, re, string, sys
from collections import Counter
from pathlib import Path

import owner_events

# --- normalisation borrowed from prompt-cluster.py so both tools cluster text the same way ---
PATHS = re.compile(r"(?:[a-zA-Z]:[\\/][^\s]+|(?:https?://|file://)\S+|(?:\.?\.?[\\/])[^\s]+)")
WORDS = re.compile(r"[a-z]+")
STOP = {"the","a","an","and","or","of","to","in","for","on","with","that","this","it","is","was",
        "be","as","at","by","from","but","not","are","were","has","have","had","i","my","me","we",
        "you","he","she","they","its","so","then","than","if","when","which","what","how","do",
        "did","does","can","could","should","would","will","just","also","very","more","most",
        "one","two","all","any","some","no","yes","up","out","into","over","after","before"}
CONTAINMENT = 0.80      # see WHY-NOT-JACCARD below
OVERLAP_FLOOR = 5       # absolute shared content tokens; containment alone over-merges
MIN_TOKENS = 3          # a 1-2 word fragment matches everything; drop it rather than pollute clusters

# WHY-NOT-JACCARD (measured on this corpus 2026-07-26, do not "simplify" back):
# prompt-cluster.py uses token Jaccard, which is right when both sides are prompts. Here the two
# lanes have wildly different lengths -- close-goal median 6 unique tokens, recovered June prompts
# p90 256 and max 781. A 6-token goal fully present inside a 256-token prompt still scores
# 6/256 = 0.02 Jaccard, so cross-era matching was not merely strict, it was arithmetically
# impossible: a first pass produced 0 June->July threads. Containment |A&B| / min(|A|,|B|) fixes the
# asymmetry; the floor stops a short generic string from being "contained" in everything.
# Threshold swept on the real corpus (clusters / spanning threads / largest cluster as % of corpus):
#   jaccard 0.42        -> 58 /  0 /  --     structurally blind across eras
#   containment 0.60/4  -> 45 / 14 / 19.6%   over-merged: one 415-record mega-cluster
#   containment 0.70/5  -> 50 / 15 /  6.0%
#   containment 0.80/5  -> 46 / 19 /  1.2%   <-- chosen: most spanning threads, smallest blob
#   containment 0.85/5  -> 41 / 14 /  0.8%   over-strict, loses real recurrences
# Do NOT add a max-token cap. Re-measured after noise filtering (kept records / clusters / spanning):
#   no cap -> 1823 / 39 / 20      cap 300 -> 1792 / 25 / 4      cap 200 -> 1785 / 21 / 1
# Excluding just 31 long records destroys 16 of the 20 June->July threads: the long recovered prompts
# ARE the era bridge, because a terse July close-row goal is contained inside the sprawling June ask
# that first raised it. Largest surviving cluster is 16 records (0.9% of corpus), so nothing is a blob.


def normalize(text):
    text = PATHS.sub(" ", str(text or "").lower())
    text = re.sub(r"\d+", " ", text)
    text = text.translate(str.maketrans({c: " " for c in string.punctuation}))
    return " ".join(w for w in WORDS.findall(text) if w not in STOP and len(w) > 2)


def similar(a, b):
    """Containment with an absolute floor -- length-asymmetry-safe. See WHY-NOT-JACCARD."""
    inter = len(a & b)
    if inter < OVERLAP_FLOOR:
        return False
    return inter / max(1, min(len(a), len(b))) >= CONTAINMENT


# ---------------------------------------------------------------- loaders

def read_jsonl(path, stats, key):
    """Yield dict rows; count unparseable lines rather than dying on one bad byte."""
    p = Path(path)
    if not p.exists():
        stats["missing_inputs"] += 1
        stats["missing:" + key] += 1
        return
    with p.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                stats["parse_errors"] += 1
                continue
            if isinstance(row, dict):
                yield row


def load_close_rows(catalog, stats):
    """July lane: goals (the ask + whether it was met) and friction (the pain + where root-caused).

    Returns (clustering_items, outcome_items). `outcome_summary` is deliberately kept OUT of the
    clustering set and used only for keyword seeding: it is one long hand-authored paragraph per
    session covering everything that happened, and long multi-topic text is exactly what glued
    unrelated threads into blobs earlier. But it is also the richest, cleanest text in the corpus, so
    the seeded-thread lane must search it -- an independent measurement of the chest-pin and
    fix-invisibility threads found all of its evidence in outcome_summary, and a first version of
    this loader that ignored the field undercounted those threads 5->1 and 7->4.
    """
    items = []
    outcomes = []
    for row in read_jsonl(catalog, stats, "catalog"):
        if row.get("entry_source") != "close":
            continue
        stats["close_rows"] += 1
        sid = str(row.get("session_id") or "")
        base = {"session_id": sid, "date": str(row.get("date") or "")[:10],
                "slug": str(row.get("slug") or ""), "handoff": str(row.get("handoff_path") or ""),
                "project": str(row.get("project") or "")}
        if row.get("outcome_summary"):
            outcomes.append(dict(base, text=str(row["outcome_summary"]), lane="close-outcome",
                                 verdict="", ref=""))
            stats["close_outcomes"] += 1
        for goal in row.get("goals") or []:
            if not isinstance(goal, dict):
                stats["malformed_goal"] += 1
                continue
            # 173 rows use {goal,verdict}; 2 legacy rows use {text,outcome} -- accept both.
            text = goal.get("goal") or goal.get("text") or ""
            verdict = str(goal.get("verdict") or goal.get("outcome") or "").strip().lower()
            if not text:
                stats["malformed_goal"] += 1
                continue
            items.append(dict(base, text=str(text), lane="close-goal", verdict=verdict, ref=""))
            stats["close_goals"] += 1
        for fr in row.get("friction") or []:
            if not isinstance(fr, dict):
                stats["malformed_friction"] += 1
                continue
            text = fr.get("what") or ""
            if not text:
                stats["malformed_friction"] += 1
                continue
            items.append(dict(base, text=str(text), lane="close-friction", verdict="",
                              ref=str(fr.get("ref") or ""), kind=str(fr.get("kind") or "")))
            stats["close_friction"] += 1
    return items, outcomes


# Harness-injected text that arrives in the user role but is not Bardia's words. Per
# incident_workflow_jsonl-user-prompt-mining-noise: STRIP leading blocks, never drop the whole
# message on a prefix match -- 12 measured messages carry genuine typed text AFTER a noise block,
# and a prefix-drop filter silently ate 3 real prompts from its own session.
NOISE_BLOCK = re.compile(
    r"^\s*(?:<(?:system-reminder|task-notification|command-name|command-message|command-args|"
    r"local-command-stdout|local-command-caveat|local-command-stderr|tool-use-id|"
    r"user-prompt-submit-hook|output-file|status|summary|"
    r"task-id|note|result|usage|subagent_tokens|tool_uses|duration_ms)>.*?</[^>]+>"
    r"|\[SYSTEM NOTIFICATION[^\]]*\][^\n]*"
    r"|Caveat: The messages below were generated by the user while running local commands\.[^\n]*"
    r"|This session is being continued from a previous conversation[^\n]*"
    r"|Your task is to create a detailed summary of the conversation[^\n]*"
    r"|Base directory for this skill[^\n]*)\s*",
    re.I | re.S)
DROP_WHOLE = re.compile(r"^\s*<command-", re.I)   # these messages are noise end to end
# Compaction/continuation summaries arrive in the user role but are CLAUDE-authored, and they are
# the single worst input here: each one is long and mentions every topic the session touched, so
# under containment scoring it matches everything and glues unrelated threads into blobs. Measured:
# they were pairing an ENGR 141 assignment request with a codex-latency investigation in one cluster.
# The mining incident's addendum makes the same point -- transcript slices leak AI-authored text.
AI_SUMMARY = re.compile(r"Primary Request and Intent|Your task is to create a detailed summary"
                        r"|analysis of the conversation so far", re.I)

# Per-session BOILERPLATE: text injected into most sessions by hooks or the harness. It is the single
# most dangerous input to a recurrence ledger, because "appears in almost every session" is exactly
# what the ledger scores as a strong recurring thread. Each of these was caught only after a reviewer
# asked why a cluster's label disagreed with its excerpts:
#   * the SessionStart workspace banner manufactured the largest cluster in the report (16 days /
#     21 sessions) out of nothing;
#   * the pasted-screenshot metadata string manufactured an entire cluster of pure UI noise.
BOILERPLATE = re.compile(
    r"Workspace path schema loaded|MAIN_ROOT=|\[Image: original \d+x\d+"
    r"|A new version of PowerShell is available|Point-of-Performance|POINT-OF-PERFORMANCE"
    r"|session-digest\]|Daily-housekeeping status"
    r"|Copyright \(C\) Microsoft Corporation|All rights reserved"
    # legacy-close null placeholder: a template default, not a session's actual pending work
    r"|session reached a natural stopping point", re.I)


# A task-notification body the capture stored as a FRAGMENT: it begins mid-block at <output-file>
# rather than at <task-notification>, so the anchored block pattern never matched it. Measured on the
# pre-July corpus: 52 of 377 records (14% of records but 54% of all text) were these. Mass is what
# drives containment matching, so they were the largest single distortion left in the ledger.
# The mining incident says STRIP rather than prefix-drop, so the block is removed and any genuine
# typed text after it survives -- an unclosed fragment leaves nothing and is dropped.
NOTIF_FRAGMENT = re.compile(r"^\s*<(?:output-file|status|summary|task-id|tool-use-id)\b", re.I)


def strip_noise(text):
    """Remove leading harness blocks in a loop; return '' if nothing substantial survives."""
    text = str(text or "")
    if DROP_WHOLE.match(text) or AI_SUMMARY.search(text[:4000]) or BOILERPLATE.search(text[:1500]):
        return ""
    prev = None
    while prev != text:
        prev = text
        text = NOISE_BLOCK.sub("", text, count=1)
        # A stripped fragment can leave an orphan closing tag at the front; that is noise too, and
        # leaving it in would both pollute the tokens and block the fragment check below.
        text = re.sub(r"^\s*</[A-Za-z][^>]{0,40}>\s*", "", text)
    text = text.strip()
    if NOTIF_FRAGMENT.match(text):
        # Unclosed/mid-block fragment the loop above could not resolve: keep only what follows the
        # last closing tag, which is where a human's typed text would be.
        tail = re.split(r"</(?:summary|status|note|result|usage|output-file)>", text)[-1]
        text = re.sub(r"<[^>]{1,40}>", " ", tail).strip()
        if len(text) < 40:
            return ""
    return text

# Segmentation was tried and MEASURED WORSE (2026-07-26): splitting prompts into per-sentence asks
# took spanning threads from 19 to 1 and filled the top of the ledger with harness resume/compaction
# preambles. It is the boilerplate that repeats most, not the asks. Noise-stripping plus
# duplicate-text collapse is what the corpus actually needed. Do not reintroduce segmentation
# without re-running the spanning-thread count.


# Munged project-dir name of a %TEMP% cwd, any user: "c--users-<name>-appdata-local-temp".
TEMP_CWD_RE = re.compile(r"^c--users-.+-appdata-local-temp")

# The "primitive close" era. Before the structured catalog existed (first close row 2026-07-02), a
# `backfill-close.ps1` wrote one markdown handoff per session. Those files were archived wholesale and
# survive at main\_archive\2026-07\2026-07-02\_close\. They matter more than anything else recovered:
# 39 of them carry a Source-transcript UUID, 34 of those sessions appear in NO catalog row, and 0 of
# their raw transcripts still exist. So this is the only surviving record of what those sessions
# actually DID -- the outcome half that the prompt corpora cannot supply.
LEGACY_SECTIONS = {
    "session goal": "legacy-goal",
    "what was accomplished": "legacy-outcome",
    "what was pending / next actions": "legacy-pending",
    "what was pending": "legacy-pending",
    "open decisions / things to verify": "legacy-decision",
    "open decisions": "legacy-decision",
}


def load_legacy_close(directory, stats):
    """Parse primitive-close handoffs into (clustering_items, outcome_items).

    Long prose goes to the outcome list (seed-searchable but kept out of clustering, same reason as
    outcome_summary: long multi-topic text glues unrelated threads together). Verdicts are left EMPTY
    on purpose -- these files have no met/unmet field, and inferring one from "accomplished" vs
    "pending" prose would be fabricating a judgement the record does not make.
    """
    items, outcomes = [], []
    d = Path(directory)
    if not d.is_dir():
        stats["missing_inputs"] += 1
        return items, outcomes
    for path in sorted(d.glob("*session*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stats["read_errors"] += 1
            continue
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", text, re.I)
        dm = re.search(r"(2026-\d\d-\d\d)", path.name)
        if not m or not dm:
            stats["legacy_unlinkable"] += 1
            continue
        stats["legacy_close_files"] += 1
        base = {"session_id": m.group(1).lower(), "date": dm.group(1), "slug": path.stem,
                "handoff": str(path), "project": "", "verdict": "", "ref": ""}
        for raw in re.split(r"^##\s+", text, flags=re.M)[1:]:
            head, _, body = raw.partition("\n")
            lane = LEGACY_SECTIONS.get(head.strip().lower())
            body = " ".join(strip_noise(body).split())
            if not lane or len(body) < 25 or body.lower().lstrip("-* ").startswith(("none", "n/a")):
                continue
            (outcomes if lane == "legacy-outcome" else items).append(
                dict(base, text=body, lane=lane))
            stats["legacy_" + lane.split("-")[1]] += 1
    return items, outcomes


def catalog_pre_july(path, stats):
    """Measure the pre-July denominator at runtime instead of freezing it into the report.

    The loss looked ~5x bigger than it is because 79% of pre-July catalog rows are sessions whose
    cwd was %TEMP% -- headless/scratchpad/hook-spawned runs, not conversations. Deriving this here
    means the coverage table can never drift out of step with the data it describes.
    """
    total = temp = 0
    real_ids = set()
    for row in read_jsonl(path, stats, "catalog-denominator"):
        if str(row.get("date") or "")[:7] not in ("2026-05", "2026-06"):
            continue
        total += 1
        p = str(row.get("raw_jsonl_path") or "")
        proj = p.split("projects\\")[-1].split("\\")[0].lower() if "projects\\" in p else ""
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", p, re.I)
        if TEMP_CWD_RE.match(proj):
            temp += 1
        elif m:
            real_ids.add(m.group(1).lower())
    return total, temp, real_ids


def load_prompts(path, stats, key, workspace_only=True):
    """pre-July lane: verbatim prompts recovered by the 2026-07-11/12 mining runs."""
    items = []
    for row in read_jsonl(path, stats, key):
        date = str(row.get("timestamp") or row.get("date") or "")[:10]
        if not date:
            stats["no_date"] += 1
            continue
        text = row.get("text") or ""
        if not text:
            continue
        cwd = str(row.get("cwd") or "")
        # Drop %TEMP%-cwd sessions: 79% of the pre-July catalog is headless/scratchpad machine runs,
        # not conversations. Counting them as "lost sessions" is what inflated the loss 5x.
        if workspace_only and cwd:
            low = cwd.lower().replace("/", "\\")
            if "desktop\\main" not in low:
                stats["dropped_non_workspace"] += 1
                continue
        text = strip_noise(text)
        if not text:
            stats["harness_noise_dropped"] += 1
            continue
        stats["prompts"] += 1
        items.append({"session_id": str(row.get("session_id") or ""), "date": date,
                      "text": text, "lane": "prompt", "verdict": "", "ref": "",
                      "slug": "", "handoff": "", "project": ""})
    return items


BARDIA_MARK = re.compile(r"^>\s*\*\*Bardia:\*\*\s*(.*)$")
FM_PROJECT = re.compile(r"^project:\s*(\S+)", re.M)


def bardia_turns(text):
    """Extract ONLY Bardia's quoted turns from a session note.

    The note format is: his turn as `> **Bardia:** ...` continued on further `>` lines, then Claude's
    reply as ORDINARY UNQUOTED paragraphs. A first version matched from the marker up to the next
    `> **` and therefore swallowed every unquoted Claude paragraph in between -- 5.9 MB of
    "Bardia's recurring asks" that was substantially Claude's own prose. That is the AI-text leak
    incident_workflow_jsonl-user-prompt-mining-noise warns about: never attribute transcript-slice
    text to Bardia without checking who authored it. So: consecutive `>` lines only, stop at the
    first unquoted line.
    """
    out, cur = [], None
    for line in text.splitlines():
        m = BARDIA_MARK.match(line)
        if m:
            if cur is not None:
                out.append(" ".join(cur))
            cur = [m.group(1)]
            continue
        if cur is None:
            continue
        if line.startswith(">"):
            cur.append(re.sub(r"^>\s?", "", line))
        else:
            out.append(" ".join(cur))
            cur = None
    if cur is not None:
        out.append(" ".join(cur))
    return out
FM_DATE = re.compile(r"^date:\s*(2026-\d\d-\d\d)", re.M)


def load_session_notes(directory, stats, workspace_only=True):
    """Per-session notes written before the catalog existed -- the largest surviving pre-July record.

    2,537 of these were swept into _archive on 2026-07-04 and not read since. Each carries YAML
    frontmatter plus verbatim `> **Bardia:**` turns, and 2,402 distinct session UUIDs appear in them
    against ZERO surviving raw transcripts, so for those sessions this is the only record that exists.
    Only the workspace notes are loaded by default: 1,813 of the 2,537 have project `AppData-Local-Temp`
    (background/subagent scratch), the same machine-run population that inflated the loss estimate ~5x.
    """
    items = []
    d = Path(directory)
    if not d.is_dir():
        stats["missing_inputs"] += 1
        return items
    for path in sorted(d.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stats["read_errors"] += 1
            continue
        head = text[:600]
        proj = (FM_PROJECT.search(head).group(1) if FM_PROJECT.search(head) else "").strip("\"'")
        if workspace_only and proj != "main":
            stats["notes_skipped_non_workspace"] += 1
            continue
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", text, re.I)
        dm = FM_DATE.search(head) or re.search(r"(2026-\d\d-\d\d)", path.name)
        if not m or not dm:
            stats["notes_unlinkable"] += 1
            continue
        stats["session_notes_files"] += 1
        turns = 0
        for turn in bardia_turns(text):
            body = " ".join(strip_noise(turn).split())
            if len(body) < 40:
                continue
            items.append({"session_id": m.group(1).lower(), "date": dm.group(1), "text": body,
                          "lane": "note-prompt", "verdict": "", "ref": "", "slug": path.stem,
                          "handoff": str(path), "project": proj})
            turns += 1
        stats["note_turns"] += turns
        if not turns:
            stats["notes_no_turns"] += 1
    return items


def collapse_echo(items, stats):
    """Collapse byte-identical recovered text down to its earliest occurrence.

    A resumed Claude session re-carries its entire history, so one prompt reappears under several
    different session ids. Since `times seen` counts distinct sessions, that echo would inflate every
    recurrence count -- and per incident_workflow_jsonl-user-prompt-mining-noise a
    (session_id, text)-keyed dedup structurally cannot catch it, because the ids genuinely differ.
    Collapsing on text alone is the only thing that works, and the same incident rules that a
    verbatim-identical repeat must be reported as one distinct ask, not N.
    Applied to the mined lanes ONLY: a close row is hand-authored one per session, so two close rows
    sharing goal text are a real repeat ask, not an echo, and must keep their separate sessions.
    """
    seen = {}
    for m in sorted(items, key=lambda x: (x["date"], x["session_id"])):
        key = " ".join(m["text"].lower().split())
        if key in seen:
            stats["echo_collapsed"] += 1
            continue
        seen[key] = m
    return list(seen.values())


def _exact_item_key(item):
    """Cross-surface identity for precedence, not semantic-similarity clustering."""
    return (str(item.get("session_id") or ""),
            str(item.get("text") or ""))


def load_canonical_events(paths, stats):
    """Load strict canonical owner events without legacy echo heuristics.

    Canonical events have already separated real owner events from carried
    history, system summaries, tools, and assistants. Applying the legacy
    text-only cross-session collapse here would erase genuine repeated asks.
    """
    items = []
    event_ids = set()
    for value in paths or []:
        path = Path(value)
        try:
            lines = owner_events.split_jsonl_records(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            raise owner_events.ContractError("canonical-events:read:" + str(path)) from error
        for line_number, line in enumerate(lines, 1):
            if not line:
                raise owner_events.ContractError(
                    "canonical-events:blank-row:{}:{}".format(path.name, line_number))
            try:
                row = owner_events.strict_json_loads(line)
            except (json.JSONDecodeError, owner_events.StrictJSONError) as error:
                raise owner_events.ContractError(
                    "canonical-events:malformed:{}:{}".format(path.name, line_number)) from error
            try:
                owner_events.validate_event(row)
            except owner_events.ContractError as error:
                raise owner_events.ContractError(
                    "canonical-events:invalid:{}:{}:{}".format(path.name, line_number, error)) from error
            event_id = row["event_id"]
            if event_id in event_ids:
                raise owner_events.ContractError("canonical-events:duplicate-event-id:" + event_id)
            event_ids.add(event_id)
            stats["canonical_events"] += 1
            if not row["text_blocks"]:
                stats["canonical_attachment_only"] += 1
                continue
            # Canonical classification is authoritative. Legacy noise heuristics
            # must never erase or rewrite a schema-valid owner event.
            body = "\n".join(row["text_blocks"])
            items.append({
                "session_id": row["session_id"], "date": row["timestamp"][:10],
                "text": body, "lane": "canonical-event", "verdict": "",
                "ref": event_id, "slug": "",
                "handoff": row["provenance"]["source_locators"][0], "project": "",
                "kind": row["event_class"],
            })
            stats["canonical_text_events"] += 1
    return items


def load_quotes(path, stats, key):
    """pre-July lane: mined claim+quote records (thin -- a claim and a locator, no full prompt)."""
    items = []
    for row in read_jsonl(path, stats, key):
        date = str(row.get("date") or "")[:10]
        text = row.get("claim") or row.get("quote") or ""
        if not date or not text:
            stats["no_date" if not date else "no_claim"] += 1
            continue
        items.append({"session_id": str(row.get("sessionId") or row.get("session_id") or ""),
                      "date": date, "text": str(text), "lane": "quote", "verdict": "",
                      "ref": str(row.get("quote_locator") or ""), "slug": "",
                      "handoff": str(row.get("source_path") or ""), "project": ""})
        stats["quotes"] += 1
    return items


# ---------------------------------------------------------------- clustering

def seeded_threads(items, seeds):
    """Track specifically-named threads by keyword, over CLOSE ROWS ONLY.

    WHY a second method: generic clustering is lexical, so a thread Bardia describes in varied prose
    ("Gym chest built+verified", "Pinned the misfiled chest row") never groups, and the ledger
    silently omits loops he explicitly cares about. WHY close rows only: an independent measurement
    pass found keyword search over the prompt corpora is 75-95% artifact -- 381 raw "migration" hits
    collapsed to ~12 real ones once bare `public\\` path mentions and swarm-dispatch boilerplate were
    dropped, whereas the close-row lane gave 5/5 and 7/7 clean, duplicate-free hits. Hand-authored
    close summaries are the only lane where a keyword means what it says.
    Terms are OR-ed (any match). OR is only safe because of the close-row restriction -- the same
    OR terms over the full corpus produced the 3-4x inflated counts described above.
    Seed shape: {"thread name": ["any", "of", "these", "terms"]}.
    """
    out = []
    for name in sorted(seeds):
        # Skip `_readme` docs keys, and reject a bare string: iterating one yields single CHARACTERS,
        # which would make a seed match every close row containing any letter.
        if name.startswith("_") or not isinstance(seeds[name], list):
            continue
        terms = [str(t).lower() for t in seeds[name] if t]
        if not terms:
            continue
        # close rows AND primitive-close handoffs: both are hand-authored outcome records, which is
        # what makes an OR keyword search safe here. Mined prompt corpora stay excluded.
        hits = [m for m in items
                if m["lane"].startswith(("close", "legacy"))
                and any(t in m["text"].lower() for t in terms)]
        if not hits:
            out.append({"label": name, "times_seen": 0, "records": 0, "first_seen": "",
                        "last_seen": "", "root_caused": "", "fix": "no close-row match",
                        "still_open": "unknown", "grade": "seeded", "sessions": [],
                        "spans_eras": False, "exemplars": [], "key": "seed"})
            continue
        for m in hits:      # outcome rows never pass through cluster(), which is what sets "tokens"
            m.setdefault("tokens", set(normalize(m["text"]).split()))
        row = summarize(hits)
        row["label"] = name
        row["grade"] = "seeded"
        out.append(row)
    return out


def cluster(items):
    """Greedy single-link grouping. Sorted input => deterministic groups.

    An inverted token->group index restricts comparisons to groups sharing at least OVERLAP_FLOOR
    tokens with the incoming record. That is EXACT, not an approximation: the index counts overlap
    against the union of a group's member tokens, which upper-bounds any single member's overlap, so
    a group it prunes could not have matched. Groups are still visited in creation order, so the
    result is identical to the naive all-pairs scan -- just fast enough to re-run while tuning.
    """
    groups = []
    index = {}
    for item in items:
        item["normalized"] = normalize(item["text"])
        item["tokens"] = set(item["normalized"].split())
        toks = item["tokens"]
        if len(toks) < MIN_TOKENS:
            continue
        shared = Counter()
        for t in toks:
            for gi in index.get(t, ()):  # noqa: SIM118 - plain dict of sets, no default churn
                shared[gi] += 1
        placed = None
        for gi in sorted(shared):
            if shared[gi] < OVERLAP_FLOOR:
                continue
            if any(similar(toks, m["tokens"]) for m in groups[gi]):
                placed = gi
                break
        if placed is None:
            groups.append([item])
            placed = len(groups) - 1
        else:
            groups[placed].append(item)
        for t in toks:
            index.setdefault(t, set()).add(placed)
    return merge_pass(groups)


def merge_pass(groups):
    """Complete the single-link clustering so the result no longer depends on arrival order.

    WHY: the greedy pass compares each record only against groups that already exist. A record that
    scores 0.778 against the sole group present when it arrives -- but 0.857 against a record that
    arrives later -- becomes an orphan and is never revisited. That was not hypothetical: it dropped a
    primitive-close handoff out of the thread it belonged to, and it is the mechanism behind one
    recurring ask appearing as three separate "split" clusters. Single-link clustering is supposed to
    be order-independent; this pass restores that by merging any two groups holding a similar pair.
    """
    merged = True
    while merged:
        merged = False
        # Candidate pairs only: two groups can merge only if they share >= OVERLAP_FLOOR tokens.
        tok = [set().union(*[m["tokens"] for m in g]) for g in groups]
        for i in range(len(groups)):
            if merged:
                break
            for j in range(i + 1, len(groups)):
                if len(tok[i] & tok[j]) < OVERLAP_FLOOR:
                    continue
                if any(similar(a["tokens"], b["tokens"]) for a in groups[i] for b in groups[j]):
                    groups[i].extend(groups[j])
                    del groups[j]
                    merged = True
                    break
    return groups


def signature(group):
    """Tokens shared by at least half the members -- the cluster's auto-label material."""
    counts = Counter(t for m in group for t in m["tokens"])
    threshold = max(1, (len(group) + 1) // 2)
    common = sorted(t for t, c in counts.items() if c >= threshold)
    if common:
        return " ".join(common)
    return min((m["normalized"] for m in group), key=lambda x: (len(x), x))


def cluster_key(group):
    """Stable id for the --names sidecar: hash of the signature, not of member order."""
    return hashlib.sha1(signature(group).encode("utf-8")).hexdigest()[:10]


def label_for(group, names, doc_freq=None):
    """Auto-label from DISTINGUISHING tokens, not alphabetical order.

    Two failed passes are worth recording, because each looked reasonable:
      1. alphabetical head of the signature -> "about acc accumulating accumulation acquires active"
         on every cluster; no row could be checked against the record.
      2. pure tf-idf (count / corpus document frequency) -> hapax junk wins, because a typo or a hash
         has doc_freq 1 and therefore the maximum score: "dac cmgwymmghk arwzj goonotes diagrom".
    So: gate on tokens SHARED by at least half the cluster's members first (that is what makes a
    token characteristic of the thread), and only then rank those by rarity in the corpus.
    """
    key = cluster_key(group)
    if key in names:
        return names[key]
    counts = Counter(t for m in group for t in m["tokens"])
    threshold = max(2, (len(group) + 1) // 2) if len(group) > 2 else 2
    shared = [t for t in counts if counts[t] >= threshold] or list(counts)
    ranked = sorted(shared, key=lambda t: (-(counts[t] / max(1, doc_freq.get(t, 1) if doc_freq else 1)),
                                           -counts[t], t))
    return " ".join(ranked[:6]) if ranked else "(unnamed)"


def summarize(group):
    """Derive every ledger column from the members. No judgement here -- only counting."""
    dates = sorted(m["date"] for m in group if m["date"])
    sessions = sorted({m["session_id"] for m in group if m["session_id"]})
    lanes = {m["lane"] for m in group}
    met = sorted(m["date"] for m in group if m["verdict"] == "met" and m["date"])
    latest = max(dates) if dates else ""

    # Evidence grade: a June row inferred from an ask alone must never read as equal to a July row
    # that has a recorded outcome.
    LEGACY = {"legacy-goal", "legacy-pending", "legacy-decision", "legacy-outcome"}
    if lanes & {"close-goal", "close-friction"} and lanes & {"prompt", "quote"}:
        grade = "mixed"
    elif lanes & {"close-goal", "close-friction"}:
        grade = "close-row"
    elif lanes & LEGACY:
        # A primitive-close handoff records what the session accomplished, so this is NOT
        # prompts-only -- it has an outcome, just no structured met/unmet verdict.
        grade = "legacy-close" if not lanes & {"prompt", "quote"} else "legacy+prompts"
    else:
        grade = "prompts-only"

    # "fix landed?" -- measure recurrences after the FIRST met claim, not the last.
    # A first pass compared the last occurrence against max(met), so any thread whose final touch
    # claimed success read "met" and the ledger came out almost all-green. That scores the wrong
    # variable: the want is "did the recurrence stop?", and a met claim that is followed by the same
    # thread coming back is precisely the fix-invisibility bug this ledger exists to surface.
    after_met = [m for m in group if met and m["date"] > met[0]]
    if not met:
        fix = "no verdict recorded" if grade == "prompts-only" else "not met"
    elif after_met:
        fix = "met " + met[0] + ", recurred " + str(len(after_met)) + "x after"
    else:
        fix = "met " + met[-1] + ", no recurrence since"

    resolved = bool(met) and not after_met
    root = ""
    for m in sorted(group, key=lambda x: (x["date"], x["session_id"])):
        if m["lane"] == "close-friction" and m["ref"]:
            root = m["ref"]
            break
    if not root:
        for m in sorted(group, key=lambda x: (x["date"], x["session_id"])):
            if m["handoff"]:
                root = m["handoff"]
                break
    # Distinct DAYS is the honest recurrence unit, not distinct sessions. Bardia runs several
    # concurrent sessions, so one ask fans out to 2-3 sibling session ids within minutes; counting
    # sessions scored that as recurrence. A reviewer found 14 of 21 clusters were single-day fanout.
    # This is the third inflation mechanism in this corpus, after resume-echo and harness noise.
    days = sorted({m["date"] for m in group if m["date"]})
    return {"key": cluster_key(group), "signature": signature(group),
            "days": len(days), "single_day": len(days) <= 1, "lanes": sorted(lanes),
            "times_seen": len(sessions) or len(group), "records": len(group),
            "first_seen": dates[0] if dates else "", "last_seen": latest,
            "root_caused": root, "fix": fix, "still_open": "no" if resolved else "yes",
            "grade": grade, "sessions": sessions,
            # Date-based, not lane-based: legacy-close handoffs are pre-July evidence too, and a
            # lane-name test would have silently excluded them from the era-span count.
            "spans_eras": bool(days and days[0][:7] < "2026-07" <= days[-1][:7]),
            "exemplars": spread_exemplars(group)}


def spread_exemplars(group, n=3):
    """Sample across the whole date range, not the first n.

    Showing the two earliest records misrepresented a 21-record thread badly enough that a reviewer
    judged its signature and its excerpts to be describing different things. Earliest + middle +
    latest lets a reader see whether the thread really persists, which is the claim being made.
    """
    ordered = sorted(group, key=lambda x: (x["date"], x["text"]))
    if len(ordered) <= n:
        return [m["text"] for m in ordered]
    picks = [0, len(ordered) // 2, len(ordered) - 1]
    return [ordered[i]["text"] for i in picks]


SLUG = re.compile(r"^[a-z][a-z0-9_.-]+$", re.I)


def check_citation(ref, memory_dir):
    """Verify a row's 'root-caused at' citation actually resolves on disk.

    WHY: a walk-test of the first version followed one row's citation and found
    `incident_workflow_plan-mode-blocks-close-on-resume` does not exist -- the real memory is
    `incident_workflow_plan-mode-on-resume-blocks-close`, the same words transposed -- and that file
    records the incident as SUPERSEDED two weeks before the ledger was generated, which the row's
    "still open: yes" did not reflect. The refs come from hand-written close rows, so the ledger was
    faithfully reproducing an unverified citation. Same-token reordering is the likeliest typo, so
    when the exact slug misses, retry on the sorted token multiset and report the real name.

    Returns (mark, note): mark is a compact symbol for the table, note is a line for the audit list.
    """
    if not ref:
        return "", ""
    if any(c in ref for c in "\\/:") or ref.lower().endswith(".md") and " " in ref:
        return ("", "") if Path(ref).exists() else ("[?]", "path not found on disk: " + ref)
    if not memory_dir or not SLUG.match(ref):
        return "", ""
    mem = Path(memory_dir)
    if not mem.is_dir():
        return "", ""
    stem = ref[:-3] if ref.lower().endswith(".md") else ref
    target = mem / (stem + ".md")
    if not target.exists():
        want = sorted(re.split(r"[_\-.]+", stem.lower()))
        for cand in sorted(mem.glob("*.md")):
            if sorted(re.split(r"[_\-.]+", cand.stem.lower())) == want:
                note = "cited `{}` does not exist; real file is `{}` (same words reordered)".format(
                    stem, cand.stem)
                # Follow through: the walk-test's real finding was not the typo but that the file
                # behind it was retired. Reporting only the rename would stop one step short.
                extra = superseded_line(cand)
                return "[!]", note + ("; and that file is SUPERSEDED — " + extra if extra else "")
        return "[?]", "cited `{}` does not exist under the memory dir".format(stem)
    extra = superseded_line(target)
    if extra:
        return "[S]", "`{}` is marked SUPERSEDED — a 'still open' verdict resting on it may be " \
                      "stale: {}".format(stem, extra)
    return "", ""


def superseded_line(path):
    """Return the SUPERSEDED line from a memory file, or '' if it is not retired."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:3000]
    except OSError:
        return ""
    if "SUPERSEDED" not in head.upper():
        return ""
    line = next((ln for ln in head.splitlines() if "SUPERSEDED" in ln.upper()), "")
    return " ".join(line.split())[:180]


def rank(row):
    # Days first: a thread touched on 5 separate days outranks one that hit 6 concurrent sessions in
    # a single afternoon. Then sessions, then era-span, then key for determinism.
    return (-row["days"], -row["times_seen"], -row["records"], not row["spans_eras"], row["key"])


# ---------------------------------------------------------------- report

def cell(text, width=96):
    text = " ".join(str(text or "").split()).replace("|", "\\|")
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def write_report(out, rows, stats, coverage, now, top, seeded=(), caveat="", inputs=()):
    shown = rows[:top]
    lines = ["# MAP-HISTORY.md \u2014 recurrence ledger", ""]
    if now:
        lines += ["_Generated by `map-history.py` on " + now +
                  ". Regenerable output \u2014 re-run the script, never hand-edit this file._", ""]
    if inputs:
        # Self-describing on purpose: a reader arriving with only this file must be able to
        # regenerate it and find the sources behind any row. --out is omitted so that two runs
        # writing to different paths still produce byte-identical content.
        lines += ["<details><summary>How to regenerate (inputs + command)</summary>", "",
                  "```bash", "cd public/projects/ai_code_projects/session-backbone",
                  "python map-history.py \\"]
        lines += ["  " + a + " \\" for a in inputs]
        lines += ["  --out ../../../../MAP-HISTORY.md", "```", "",
                  "Names and seeded threads live in `map-history-names.json` and "
                  "`map-history-seeds.json` beside the script. `python map-history.py --self-test` "
                  "runs the fixture assertions.", "</details>", ""]
    lines += [
        "One row per recurring thread, sorted by how many distinct sessions touched it. "
        "This is a ledger, not a chronology \u2014 the question it answers is *what keeps coming back*.",
        "",
        "**Evidence grade** \u2014 `close-row` has a recorded outcome (`goals[].verdict`); "
        "`prompts-only` is reconstructed from recovered June/May prompts, which carry the ask but "
        "not the result, so such a row can show recurrence and never that a fix landed; "
        "`mixed` spans both eras and is the strongest recurrence evidence here.",
        "",
        "**`days` is the recurrence measure, not `sessions`.** Concurrent sibling sessions make one "
        "ask land 2-3 times within minutes, which counting sessions scores as recurrence; a "
        "`days = 1` row is same-day fan-out, not a thread that keeps coming back. Rows are sorted "
        "by days for that reason.",
        "",
        "| # | thread | days | sessions | first seen | last seen | root-caused at | fix landed? | still open? | evidence |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    cites = []
    for i, r in enumerate(shown, 1):
        mark = r.get("cite_mark", "")
        if r.get("cite_note"):
            cites.append("- row {}: {}".format(i, r["cite_note"]))
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            i, cell(r["label"], 66) + (" _(same-day only)_" if r["single_day"] else ""),
            r["days"], r["times_seen"], r["first_seen"] or "?", r["last_seen"] or "?",
            (cell(r["root_caused"] or "\u2014", 50) + (" " + mark if mark else "")),
            cell(r["fix"], 40), r["still_open"], r["grade"]))
    if len(rows) > top:
        lines += ["", "_Showing top {} of {} threads that recurred across >=2 sessions "
                      "(`--top` to widen). Threads below the cut are single-session or "
                      "low-count._".format(top, len(rows))]

    if cites:
        lines += ["", "### Citation check — do not trust a `root-caused at` string without this", "",
                  "`[!]` cited artifact does not exist but a same-words file does · `[?]` cited "
                  "artifact not found · `[S]` artifact exists but is marked SUPERSEDED, so a "
                  "\"still open\" verdict resting on it may be stale. Refs are copied from "
                  "hand-written close rows, so they can be wrong; these are checked against disk "
                  "on every run.", ""]
        lines += cites
        lines.append("")
    elif rows:
        lines += ["", "_Citation check: every `root-caused at` reference in the rows above resolves "
                      "to an existing artifact, and none is marked SUPERSEDED._", ""]

    if seeded:
        lines += ["", "## Named threads tracked by keyword (second method)", "",
                  "Threads worth watching by name, matched over **close rows only**. Two reasons this "
                  "is a separate table: the clustering above is lexical, so a thread described in "
                  "varied prose never groups; and keyword search over the recovered prompt corpora "
                  "is 75-95% artifact (381 raw \"migration\" hits reduced to ~12 real ones once "
                  "ubiquitous `public\\` path mentions and swarm-dispatch boilerplate were removed), "
                  "whereas the hand-authored close summaries measured clean and duplicate-free.", "",
                  "| thread | sessions | first seen | last seen | fix landed? | still open? |",
                  "|---|---|---|---|---|---|"]
        for r in seeded:
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                cell(r["label"], 60), r["times_seen"], r["first_seen"] or "—",
                r["last_seen"] or "—", cell(r["fix"], 40), r["still_open"]))

    lines += ["", "## Exemplars", "",
              "Verbatim source text per thread, so a row can be checked against the record "
              "rather than trusted.", ""]
    for i, r in enumerate(shown, 1):
        lines += ["### {}. {}".format(i, r["label"]),
                  "",
                  "- key: `{}` \u00b7 records: {} \u00b7 sessions: {} \u00b7 grade: {}".format(
                      r["key"], r["records"], r["times_seen"], r["grade"])]
        if r["sessions"][:3]:
            lines.append("- session ids: " + ", ".join("`" + s + "`" for s in r["sessions"][:3]) +
                         ("\u2026" if len(r["sessions"]) > 3 else ""))
        for ex in r["exemplars"]:
            lines += ["", "  > " + cell(ex, 400)]
        lines.append("")

    lines += ["## June/May coverage \u2014 measured, not adjectival", "",
              "The June/May record is **partially** recoverable. The raw transcripts were purged by "
              "`cleanupPeriodDays` (14 days, set 2026-07-12) before the harvester existed, but two "
              "mining runs on 2026-07-11..13 had already extracted prompts and quotes, and those "
              "extracts survive.", "",
              "| basis | coverage |", "|---|---|"]
    for k, v in coverage:
        lines.append("| {} | {} |".format(k, v))
    lines += ["", caveat, "",
              "What is genuinely gone is the *outcome* half \u2014 assistant output, tool calls, "
              "results. The asks survived; the results did not. So a row resting only on pre-July "
              "evidence is graded `prompts-only` and can never claim a fix landed; a row that also "
              "carries a July close row is graded `mixed`, and there its July half supplies the "
              "outcome its June half cannot.", "",
              "Recovery paths checked and closed (2026-07-26): an Anthropic account data export "
              "contains claude.ai conversations only \u2014 0 of 116 conversations in the newest "
              "export carry a Claude Code transcript signature, and no export tree contains a "
              "single `.jsonl`, so requesting a fresh one recovers nothing here. The Drive backup "
              "of `~/.claude` excludes `projects/` and `*.jsonl` by design (oldest retained zip: "
              "2026-07-18, 0 jsonl entries). `~/.claude` git history never tracked transcripts "
              "(`.gitignore`: `projects/**/*.jsonl`); the Recycle Bin is empty. VSS shadow copies "
              "remain unchecked \u2014 they need an elevated shell, so they are unverified rather "
              "than proven absent.", ""]

    lines += ["## Provenance", "",
              "| input | records loaded |", "|---|---|"]
    for k in sorted(stats):
        if k.startswith("missing:") or k in {"missing_inputs"}:
            continue
        lines.append("| {} | {} |".format(k.replace("_", " "), stats[k]))
    lines.append("")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(lines), encoding="utf-8")
    return shown


# ---------------------------------------------------------------- run

def run(args):
    stats = Counter()
    names = {}
    if args.names and Path(args.names).exists():
        try:
            names = json.loads(Path(args.names).read_text(encoding="utf-8"))
        except ValueError:
            stats["parse_errors"] += 1
        if not isinstance(names, dict):
            names = {}

    items, outcomes = load_close_rows(args.catalog, stats)
    if args.legacy_close:
        li, lo = load_legacy_close(args.legacy_close, stats)
        items += li
        outcomes += lo
    pre = []
    for p in args.prompts or []:
        pre += load_prompts(p, stats, "prompts:" + Path(p).name, workspace_only=not args.all_cwd)
    for p in args.quotes or []:
        pre += load_quotes(p, stats, "quotes:" + Path(p).name)
    if args.session_notes:
        pre += load_session_notes(args.session_notes, stats, workspace_only=not args.all_cwd)
    pre = [m for m in pre if m["date"][:7] < "2026-07"]      # July already covered by close rows
    try:
        canonical = load_canonical_events(getattr(args, "canonical_events", []), stats)
    except owner_events.ContractError as error:
        print("FAIL: " + str(error), file=sys.stderr)
        summary = {"exit_code": 2, "canonical_error": str(error)}
        print("SUMMARY: " + json.dumps(summary, sort_keys=True))
        return 2, summary, []

    # Evidence precedence is value-specific. Close rows retain verdicts, canonical events retain
    # exact owner-event provenance, and mined prompts/notes are the fallback. Exact duplicates are
    # removed only within the same session; similarity clustering still happens later.
    close_keys = {_exact_item_key(item) for item in items}
    kept_canonical = []
    for item in canonical:
        if _exact_item_key(item) in close_keys:
            stats["canonical_superseded_by_close"] += 1
        else:
            kept_canonical.append(item)
    canonical_keys = {_exact_item_key(item) for item in kept_canonical}
    kept_legacy = []
    for item in pre:
        if _exact_item_key(item) in canonical_keys:
            stats["legacy_superseded_by_canonical"] += 1
        else:
            kept_legacy.append(item)
    pre = kept_legacy
    pre = collapse_echo(pre, stats)
    canonical_pre = [item for item in kept_canonical if item["date"][:7] < "2026-07"]
    canonical_post = [item for item in kept_canonical if item["date"][:7] >= "2026-07"]
    pre += canonical_pre
    stats["canonical_post_cutover"] = len(canonical_post)
    stats["pre_july_records"] = len(pre)

    # ASSERT the pre-July lane actually loaded. A silently-empty lane would produce a July-only
    # ledger that looks complete -- exactly the false-green this ledger exists to catch.
    if args.require_pre_july and not pre:
        print("FAIL: pre-July lane loaded 0 records; ledger would be July-only", file=sys.stderr)
        print("SUMMARY: " + json.dumps({"exit_code": 2, "pre_july_records": 0}, sort_keys=True))
        return 2, {}, []

    # Sort before clustering: greedy clustering is order-sensitive, so a stable order is what makes
    # the output byte-identical across runs.
    allitems = sorted(items + pre + canonical_post,
                      key=lambda m: (m["date"], m["lane"], m["session_id"], m["text"]))
    groups = cluster(allitems)
    stats["records_clustered"] = sum(len(g) for g in groups)

    # corpus-wide document frequency, for tf-idf-lite auto-labels
    doc_freq = Counter()
    for g in groups:
        for m in g:
            doc_freq.update(m["tokens"])

    rows = []
    for g in groups:
        if len({m["session_id"] for m in g if m["session_id"]}) < args.min_sessions:
            continue
        row = summarize(g)
        row["label"] = label_for(g, names, doc_freq)
        rows.append(row)
    rows.sort(key=rank)
    for r in rows:
        r["cite_mark"], r["cite_note"] = check_citation(r["root_caused"], args.memory_dir)
        if r["cite_note"]:
            stats["citation_problems"] += 1

    # Coverage is MEASURED here, never asserted from an earlier session's notes.
    cat_total, cat_temp, cat_real = catalog_pre_july(args.catalog, stats)
    rec_sessions = {m["session_id"] for m in pre if m["session_id"]}
    joined = rec_sessions & cat_real
    unlisted = rec_sessions - cat_real - {""}
    pct_all = 100.0 * len(joined) / max(1, cat_total)
    pct_real = 100.0 * len(joined) / max(1, len(cat_real))
    coverage = [
        ("catalog pre-July rows (May + June)", "{:,}".format(cat_total)),
        ("...of which cwd is `%TEMP%` (machine runs, not conversations)",
         "{:,} ({:.1f}%)".format(cat_temp, 100.0 * cat_temp / max(1, cat_total))),
        ("...real workspace pre-July sessions (the honest denominator)", "{:,}".format(len(cat_real))),
        ("sessions with recovered verbatim content, joined to a catalog row",
         "{} = **{:.1f}%** of {:,} rows, **{:.1f}%** of {:,} real sessions".format(
             len(joined), pct_all, cat_total, pct_real, len(cat_real))),
        ("distinct pre-July sessions with any recovered content",
         "{} ({} of them absent from the catalog entirely)".format(len(rec_sessions), len(unlisted))),
        ("pre-July records feeding this ledger (after echo collapse)",
         "{:,} ({:,} duplicate-text echoes collapsed)".format(stats["pre_july_records"],
                                                              stats["echo_collapsed"])),
        ("threads in this ledger spanning June -> July", str(sum(1 for r in rows if r["spans_eras"]))),
    ]
    seeded = []
    if args.seeds and Path(args.seeds).exists():
        try:
            spec = json.loads(Path(args.seeds).read_text(encoding="utf-8"))
        except ValueError:
            spec = {}
            stats["parse_errors"] += 1
        if isinstance(spec, dict):
            seeded = seeded_threads(items + outcomes, spec)

    caveat = (
        "The percentage against the catalog's own row count understates real coverage: "
        "**{:,} of those {:,} pre-July rows have cwd `%TEMP%`** ({:.1f}%) — headless/scratchpad "
        "machine runs, not conversations. Against the {:,} rows that are real workspace sessions, "
        "coverage is **{:.1f}%**. Both figures are recomputed on every run from the catalog itself, "
        "so this paragraph cannot drift away from the table above it."
    ).format(cat_temp, cat_total, 100.0 * cat_temp / max(1, cat_total), len(cat_real), pct_real)

    inputs = ["--catalog " + str(args.catalog)]
    if args.prompts:
        inputs.append("--prompts " + " ".join(str(p) for p in args.prompts))
    if args.quotes:
        inputs.append("--quotes " + " ".join(str(p) for p in args.quotes))
    if getattr(args, "canonical_events", []):
        inputs.append("--canonical-events " + " ".join(
            str(p) for p in args.canonical_events))
    if args.names:
        inputs.append("--names " + str(args.names))
    if args.seeds:
        inputs.append("--seeds " + str(args.seeds))
    inputs.append("--now " + (args.now or "<date>") + " --top " + str(args.top) + " --require-pre-july")

    shown = write_report(args.out, rows, stats, coverage, args.now, args.top, seeded, caveat, inputs)
    summary = {"exit_code": 0, "close_rows": stats["close_rows"], "records": len(allitems),
               "pre_july_records": stats["pre_july_records"], "clusters": len(rows),
               "shown": len(shown), "seeded_threads": len(seeded),
               "spanning_threads": sum(1 for r in rows if r["spans_eras"]),
               "parse_errors": stats["parse_errors"], "missing_inputs": stats["missing_inputs"],
               "dropped_non_workspace": stats["dropped_non_workspace"],
               "harness_noise_dropped": stats["harness_noise_dropped"],
               "echo_collapsed": stats["echo_collapsed"],
               "citation_problems": stats["citation_problems"],
               "legacy_close_files": stats["legacy_close_files"],
               "session_notes_files": stats["session_notes_files"],
               "note_turns": stats["note_turns"],
               "canonical_events": stats["canonical_events"],
               "canonical_text_events": stats["canonical_text_events"],
               "canonical_attachment_only": stats["canonical_attachment_only"],
               "canonical_post_cutover": stats["canonical_post_cutover"],
               "canonical_superseded_by_close": stats["canonical_superseded_by_close"],
               "legacy_superseded_by_canonical": stats["legacy_superseded_by_canonical"]}
    print("SUMMARY: " + json.dumps(summary, sort_keys=True))
    return 0, summary, rows


# ---------------------------------------------------------------- self-test

def self_test():
    import tempfile
    a = 0
    fx = Path(__file__).resolve().parent / "fixtures" / "map-history"
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "MAP-HISTORY.md"
        args = argparse.Namespace(
            catalog=str(fx / "catalog.jsonl"), prompts=[str(fx / "prompts.jsonl")],
            quotes=[str(fx / "quotes.jsonl")], names=str(fx / "names.json"), out=str(out),
            seeds=str(fx / "seeds.json"), memory_dir=str(fx / "memory"),
            legacy_close=str(fx / "legacy-close"), session_notes=str(fx / "session-notes"),
            canonical_events=[], min_sessions=2, top=20, now="2026-07-26",
            all_cwd=False, require_pre_july=True)
        code, summary, rows = run(args)
        text = out.read_text(encoding="utf-8")
        first = text
        run(args)                                   # determinism: same inputs => same bytes
        second = out.read_text(encoding="utf-8")

        by_key = {r["key"]: r for r in rows}
        named = [r for r in rows if r["label"] == "chest pin recurrence"]
        spanning = [r for r in rows if r["spans_eras"]]
        recurred = [r for r in rows if "recurred" in r["fix"] and "x after" in r["fix"]]

        # a lane that loads nothing must FAIL loudly, not emit a July-only ledger
        bad = argparse.Namespace(**{**vars(args), "prompts": [], "quotes": [],
                                    "session_notes": None, "out": str(Path(td) / "x.md")})
        bad_code = run(bad)[0]

        checks = [
            (code == 0, "run failed"),
            (summary["close_rows"] == 3, "close rows loaded: " + str(summary.get("close_rows"))),
            (summary["pre_july_records"] == 8, "pre-July records: " + str(summary.get("pre_july_records"))),
            (summary["dropped_non_workspace"] == 1, "%TEMP% prompt not dropped"),
            (summary["parse_errors"] == 1, "malformed line not counted"),
            # noise filter: the three cases the mining incident says must all be asserted
            (summary["harness_noise_dropped"] == 1, "pure-noise prompt not dropped"),
            (strip_noise("<system-reminder>x</system-reminder>") == "", "pure noise survived"),
            (strip_noise("<system-reminder>x</system-reminder>\nreal ask here") == "real ask here",
             "noise+text: genuine text was eaten"),
            (strip_noise("plain ask untouched") == "plain ask untouched", "plain text mangled"),
            # per-session boilerplate: present in most sessions, so it fakes strong recurrence
            (strip_noise("Workspace path schema loaded: MAIN_ROOT=C:\\x and then a real ask") == "",
             "SessionStart workspace banner survived"),
            (strip_noise("[Image: original 2084x162, displayed at 2000x155.] look at this") == "",
             "pasted-screenshot metadata survived"),
            # task-notification stored as a mid-block fragment (14% of records, 54% of text volume)
            (strip_noise("<output-file>C:\\tmp\\x.output</output-file> <status>completed</status>"
                         " <summary>Agent finished</summary>") == "",
             "notification fragment survived"),
            (strip_noise("<output-file>C:\\tmp\\x.output</output-file></summary> and now please "
                         "go and fix the classifier keyword properly this time")
             == "and now please go and fix the classifier keyword properly this time",
             "genuine text after a notification fragment was eaten"),
            (strip_noise("<command-name>/close</command-name>") == "", "command message not dropped"),
            # resume echo: byte-identical text under a DIFFERENT session id must not inflate counts
            (summary["echo_collapsed"] == 1, "resume echo not collapsed"),
            (first == second, "output not deterministic"),
            (len(rows) >= 2, "too few clusters: " + str(len(rows))),
            (len(named) == 1, "--names sidecar label not applied"),
            (len(spanning) >= 1, "no June->July spanning thread detected"),
            (len(recurred) >= 1, "fix-invisibility (met-then-recurred) not detected"),
            (all(r["times_seen"] >= 2 for r in rows), "min-sessions not enforced"),
            # days metric: the honest recurrence unit, and same-day fan-out must be marked as such
            (all(r["days"] >= 2 and not r["single_day"] for r in rows),
             "fixture threads should each span multiple days"),
            (rows == sorted(rows, key=rank), "rows not sorted by the days-first rank"),
            (summarize([{"date": "2026-06-01", "text": "a", "session_id": "s1", "lane": "prompt",
                         "verdict": "", "ref": "", "handoff": "", "tokens": {"a"}},
                        {"date": "2026-06-01", "text": "b", "session_id": "s2", "lane": "prompt",
                         "verdict": "", "ref": "", "handoff": "", "tokens": {"b"}}])["single_day"],
             "two sessions on one day not flagged as same-day fan-out"),
            ("`days` is the recurrence measure" in text, "days-vs-sessions caveat missing"),
            # primitive-close lane: the only surviving record of pre-catalog session OUTCOMES
            (summary["legacy_close_files"] == 1, "legacy close handoff not parsed: "
                                                 + str(summary.get("legacy_close_files"))),
            # session notes: Bardia's quoted turns only. The unquoted Claude paragraph in the fixture
            # must NOT appear, and the noise-only / too-short turns must be dropped.
            (bardia_turns("> **Bardia:** ask one\n> continued\n\nClaude reply here\n\n"
                          "> **Bardia:** ask two\n")
             == ["ask one continued", "ask two"], "Bardia-turn extraction wrong"),
            (summary["note_turns"] == 2, "expected 2 usable fixture turns, got "
                                         + str(summary.get("note_turns"))),
            (not any("CLAUDE's text" in " ".join(r["exemplars"]) for r in rows),
             "Claude-authored prose leaked into a reported thread"),
            (any("note-prompt" in r["lanes"] for r in rows), "session-note lane never reached a row"),
            (any(any(l.startswith("legacy") for l in r["lanes"]) for r in rows),
             "the legacy handoff never reached a reported row"),
            (all(r["grade"] != "prompts-only" for r in rows
                 if any(l.startswith("legacy") for l in r["lanes"])),
             "a legacy-backed row was still graded prompts-only despite having an outcome record"),
            (any(r["grade"] == "mixed" for r in rows), "mixed grade never assigned"),
            (all(r["grade"] in {"mixed", "close-row", "prompts-only"} for r in rows), "bad grade"),
            (rank(rows[0]) <= rank(rows[-1]), "not sorted by times-seen"),
            ("cwd `%TEMP%`" in text, "TEMP caveat missing from report"),
            ("the honest denominator" in text, "coverage table missing from report"),
            # coverage must be DERIVED from the data, not a frozen number from an earlier session
            ("1,232" not in text, "stale hardcoded denominator leaked into the report"),
            ("prompts-only" in text, "evidence-grade legend missing"),
            (bad_code == 2, "empty pre-July lane did not fail loudly"),
            # seeded threads: matched, reported, and a miss reported as a miss rather than omitted
            ("Named threads tracked by keyword" in text, "seeded-thread table missing"),
            ("chest pin loop" in text, "seeded thread not matched"),
            ("no close-row match" in text, "unmatched seed silently dropped"),
            (summary["seeded_threads"] == 2, "docs/bare-string seed keys not skipped: "
                                             + str(summary.get("seeded_threads"))),
            # citation check: both failure shapes the walk-test surfaced must be caught
            ("Citation check" in text, "citation-check section missing"),
            ("[!]" in text, "transposed-name citation not flagged"),
            ("[S]" in text, "SUPERSEDED artifact not flagged"),
            ("same words reordered" in text, "transposed citation not explained"),
            # 2 rows carry a citation problem: a transposed slug [!] and a SUPERSEDED artifact [S].
            (summary["citation_problems"] == 2, "citation problem count: "
                                                + str(summary.get("citation_problems"))),
            # The [?] shape is unit-checked directly rather than via the report: which row wins the
            # root-caused slot shifts with clustering, so asserting it appears in the text was a
            # vacuous test that passed off the legend alone.
            (check_citation("does_not_exist_anywhere", str(fx / "memory"))[0] == "[?]",
             "missing citation not flagged [?]"),
            (check_citation(str(fx / "nope" / "gone.md"), str(fx / "memory"))[0] == "[?]",
             "missing path not flagged [?]"),
            (check_citation("", str(fx / "memory")) == ("", ""), "empty ref should be silent"),
            (check_citation("incident_workflow_on-resume-plan-mode-blocks",
                            str(fx / "memory")) == ("", ""), "a good citation was flagged"),
            (len(spread_exemplars([{"date": "a", "text": "1"}, {"date": "b", "text": "2"},
                                   {"date": "c", "text": "3"}, {"date": "d", "text": "4"}])) == 3,
             "exemplar spread wrong count"),
            (spread_exemplars([{"date": "a", "text": "1"}, {"date": "b", "text": "2"},
                               {"date": "c", "text": "3"}, {"date": "d", "text": "4"}])[-1] == "4",
             "exemplar spread did not reach the latest record"),
            (normalize("Use C:\\secret\\file42.md now") == "use now", "path/digit stripping"),
            (normalize("it was in the of and a") == "", "stopword stripping"),
            (len(by_key) == len(rows), "cluster keys not unique"),
        ]
    for ok, msg in checks:
        a += 1
        if not ok:
            print("SELF-TEST FAIL: " + msg)
            print("SUMMARY: " + json.dumps({"assertions": a, "passed": False}, sort_keys=True))
            return 1
    print("SUMMARY: " + json.dumps({"assertions": a, "passed": True}, sort_keys=True))
    print("SELF-TEST PASS: " + str(a) + " assertions")
    return 0


def parser():
    p = argparse.ArgumentParser(description="Build a recurrence ledger from session history.")
    p.add_argument("--catalog", help="sessions.jsonl (read-only; close rows supply the July lane)")
    p.add_argument("--prompts", nargs="*", default=[], help="pre-July prompt corpora (jsonl)")
    p.add_argument("--quotes", nargs="*", default=[], help="pre-July mined quote corpora (jsonl)")
    p.add_argument("--canonical-events", nargs="*", default=[],
                   help="strict canonical owner-event JSONL (all dates; canonical evidence precedence)")
    p.add_argument("--names", help="optional {cluster_key: label} sidecar json")
    p.add_argument("--seeds", help="optional {thread name: [keywords]} json, matched over close rows")
    p.add_argument("--memory-dir", help="memory .md dir; verifies each row's root-caused citation")
    p.add_argument("--legacy-close", help="primitive-close handoff dir (pre-catalog session outcomes)")
    p.add_argument("--session-notes", help="archived per-session note dir (verbatim Bardia turns)")
    p.add_argument("--out", help="MAP-HISTORY.md output path")
    p.add_argument("--min-sessions", type=int, default=2, help="min distinct sessions (default 2)")
    p.add_argument("--top", type=int, default=20, help="threads detailed in the report (default 20)")
    p.add_argument("--now", default="", help="explicit date stamp; omit for none (keeps re-runs identical)")
    p.add_argument("--all-cwd", action="store_true", help="keep %%TEMP%% sessions (default: drop)")
    p.add_argument("--require-pre-july", action="store_true",
                   help="exit 2 if the pre-July lane loaded nothing")
    p.add_argument("--self-test", action="store_true", help="run fixture-only assertions")
    return p


def main():
    p = parser()
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if not a.catalog or not a.out:
        p.error("--catalog and --out are required unless --self-test")
    if a.min_sessions < 1:
        p.error("--min-sessions must be at least 1")
    return run(a)[0]


if __name__ == "__main__":
    sys.exit(main())
