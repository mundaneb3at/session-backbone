#!/usr/bin/env python3
"""Build a provenance-only work-state ledger from tolerant source parsing."""
import argparse, datetime, json, os, re, shutil, sys, tempfile
from collections import Counter, defaultdict
from pathlib import Path

TOKEN_RE=re.compile(r"[a-z0-9]+")

def tokens(text):
    return set(TOKEN_RE.findall(str(text or "").lower()))

def overlap(a,b):
    left,right=tokens(a),tokens(b)
    return len(left&right)/max(1,min(len(left),len(right)))

def open_count(row):
    value=row.get("open_threads",0)
    if isinstance(value,list): return len(value)
    try: return max(0,int(value))
    except (TypeError,ValueError): return 0

def verdict_name(value):
    text=str(value or "unknown").strip().lower()
    text=re.sub(r"[^a-z0-9]+","-",text).strip("-")
    return text or "unknown"

def session_threads(row,stats):
    slug=str(row["slug"]); count=row["_open_count"]; found=[]
    raw=row.get("goals")
    if isinstance(raw,str): raw=[raw]
    elif raw is None: raw=[]
    elif not isinstance(raw,list):
        stats["skipped_invalid_goals"]+=1; raw=[]
    for item in raw:
        if isinstance(item,dict):
            goal=str(item.get("goal","") or "").strip()
            if not goal:
                stats["skipped_invalid_goal_item"]+=1; continue
            verdict=verdict_name(item.get("verdict"))
            found.append({"goal":goal,"status":"claimed-"+verdict+"@"+slug,
                          "state":"met" if verdict=="met" else "unknown"})
        elif isinstance(item,str) and item.strip():
            found.append({"goal":item.strip(),"status":"claimed-unknown@"+slug,
                          "state":"unknown"})
        else: stats["skipped_invalid_goal_item"]+=1
    if not found:
        claim=row.get("status_claimed")
        claim=claim.strip() if isinstance(claim,str) else ""
        if claim:
            found.append({"goal":claim[:160],"status":"claimed-unknown@"+slug,
                          "state":"unknown"})
        else: stats["skipped_no_claim"]+=1
    if count>0:
        open_text=row.get("open_text")
        goal=open_text.strip()[:160] if isinstance(open_text,str) and open_text.strip() else "Open threads: "+str(count)
        found.append({"goal":goal,"status":"open-threads@"+slug,"state":"open",
                      "open_thread_count":count})
    return found

def load_catalog(path,stats):
    sessions=[]
    try:
        lines=Path(path).read_text(encoding="utf-8",errors="replace").splitlines()
    except OSError as e:
        stats["catalog_read_errors"]+=1; return sessions
    for line_no,line in enumerate(lines,1):
        if not line.strip(): continue
        try: row=json.loads(line)
        except (json.JSONDecodeError,TypeError):
            stats["skipped_malformed"]+=1; continue
        if not isinstance(row,dict):
            stats["skipped_non_object"]+=1; continue
        slug=str(row.get("slug","") or "").strip()
        if not slug:
            stats["skipped_missing_slug"]+=1; continue
        row=dict(row); row["slug"]=slug; row["_line"]=line_no
        row["_open_count"]=open_count(row)
        row["_threads"]=session_threads(row,stats); sessions.append(row)
    return sessions

def registry_memberships(chests,sessions,stats):
    """D1 (2026-07-22): topic membership comes from the chest generator's durable
    registry (topic-registry.json in the --chests dir), slug-joined -- NEVER from
    scraping chest prose. Navigation delisting (tombstoned sessions dropping out
    of chest listings) must not silently detag THREADS rows; prose mentions can
    neither create nor remove membership. Backfill entries may carry
    `extra_topics` for sessions that held 2+ topics in the last prose-era build."""
    found=defaultdict(set); slugs={str(s.get("slug","")) for s in sessions}
    reg=Path(chests)/"topic-registry.json"
    try: data=json.loads(reg.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError,UnicodeDecodeError):
        stats["registry_read_errors"]+=1; return found
    assignments=data.get("assignments") or {}
    stats["registry_entries"]=len(assignments)
    for entry in assignments.values():
        if not isinstance(entry,dict): continue
        slug=str(entry.get("slug","") or "")
        if not slug or slug not in slugs: continue
        extra=entry.get("extra_topics")
        for topic in [entry.get("topic")]+(extra if isinstance(extra,list) else []):
            topic=str(topic or "").strip()
            if topic: found[slug].add(topic); stats["registry_matched_topics"]+=1
    return found

def prompt_map(root,stats):
    result={}
    try: files=sorted(Path(root).glob("*.md"),key=lambda p:p.name.lower())
    except OSError: stats["prompt_read_errors"]+=1; return result
    for path in files:
        try: lines=path.read_text(encoding="utf-8",errors="replace").splitlines()
        except OSError: stats["prompt_read_errors"]+=1; continue
        stats["prompt_files"]+=1
        for line in lines[:80]:
            match=re.match(r"\s*session_id\s*:\s*(\S+)\s*$",line,re.I)
            if match:
                result[match.group(1)]=str(path); break
    return result

def _plans_root():
    # ~/.claude/plans is the ephemeral, purge-doomed home; env override = self-test hook.
    return Path(os.environ.get("THREADS_PLANS_ROOT") or (Path.home()/".claude"/"plans")).expanduser()

def _archive_root():
    # Durable sibling of the ephemeral ~/.claude/plans root; THREADS_PLANS_ARCHIVE overrides.
    return Path(os.environ.get("THREADS_PLANS_ARCHIVE") or
                (Path.home()/".claude"/"plans-archive")).expanduser()

def _canon(p):
    return os.path.normcase(str(Path(p).expanduser().resolve(strict=False)))

def _same_bytes(p,data):
    try: return p.read_bytes()==data
    except OSError: return False

def _dated_copies(name):
    """Durable-archive copies of basename `name` as (iso_date, canonical_str, Path),
    keeping only real files under a valid ISO YYYY-MM-DD subdir. Basename identifies
    one logical (slug-based) plan, so newest archive-date is the version policy. Rank
    by PARSED date -- never a raw string sort (Sol-consult 2026-07-24)."""
    out=[]
    try: hits=_archive_root().glob("*/"+name)
    except OSError: return out
    for hit in hits:
        stamp=hit.parent.name
        try: day=datetime.date.fromisoformat(stamp)
        except ValueError: continue
        if day.isoformat()==stamp and hit.is_file(): out.append((day,_canon(hit),hit))
    return out

def _atomic_copy(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=str(dst.parent),prefix=".tmp-",suffix="-"+src.name); os.close(fd)
    try:
        shutil.copy2(str(src),tmp); os.replace(tmp,str(dst))
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

def _archive_now(src):
    """Atomically COPY an alive plan into plans-archive/<today>/ (copy-if-absent,
    byte-identical), returning the durable Path. Strict mode (2026-07-24): THREADS.md
    must never publish an ephemeral ~/.claude/plans path -- it dies on the 14-day
    purge. This duplicates the nightly guard's copy primitive on purpose; idempotent
    + atomic so it never races destructively with the guard."""
    dst=_archive_root()/datetime.date.today().isoformat()/src.name
    if dst.is_file() and _same_bytes(dst,src.read_bytes()): return dst
    _atomic_copy(src,dst)
    return dst

def resolve_archive(value):
    """Resolve a handoff/transcript pointer to a DURABLE on-disk address.

    Durability boundary (2026-07-24, Sol-consult; root-caused the recurring THREADS
    invariant-2 FAIL): ~/.claude/plans is ephemeral (14-day rolling purge), so a
    pointer under it must never be published as-is -- even alive now, it dangles at
    the next purge. The prior `if path.exists(): return value` gate healed only
    ALREADY-dead pointers, so every regen re-baked soon-doomed plans-root paths.
      not under plans-root, exists           -> keep (already durable)
      not under plans-root, dead + durable   -> heal to durable copy
      not under plans-root, dead, no durable -> pass through unchanged
      under plans-root, alive, durable match -> emit the matching durable copy
      under plans-root, alive, no match      -> archive it NOW, emit durable
      under plans-root, dead + durable       -> emit newest durable copy
      under plans-root, dead, no durable     -> raise (genuinely purged + lost)"""
    path=Path(value).expanduser()
    root=_plans_root()
    try: under=os.path.commonpath((_canon(path),_canon(root)))==_canon(root)
    except ValueError: under=False   # different drive letter -> not under plans-root
    copies=_dated_copies(path.name)
    def newest(cs): return str(max(cs,key=lambda c:(c[0],c[1]))[2])
    if not under:
        if path.exists(): return value
        return newest(copies) if copies else value
    if path.is_file():
        try: live=path.read_bytes()
        except OSError as e: raise RuntimeError("cannot read ephemeral handoff: "+str(path)) from e
        match=[c for c in copies if _same_bytes(c[2],live)]
        return newest(match) if match else str(_archive_now(path))
    if copies: return newest(copies)
    raise FileNotFoundError("ephemeral handoff purged with no durable copy: "+str(path))

def pointers(session,prompts):
    values=[]; seen=set()
    for key,label in (("handoff_path","handoff"),("handoff","handoff"),
                      ("transcript_md_path","transcript"),
                      ("raw_jsonl_path","jsonl"),("jsonl","jsonl")):
        value=str(session.get(key,"") or "").strip()
        if value and label in ("handoff","transcript"):
            value=resolve_archive(value)
        pointer=label+"="+value
        if value and pointer not in seen:
            values.append(pointer); seen.add(pointer)
    session_id=str(session.get("session_id","") or "").strip()
    slug=str(session.get("slug",""))
    prompt_path=prompts.get(session_id) or prompts.get(slug)
    if prompt_path: values.append("prompt-estate="+prompt_path)
    return values

def build_rows(sessions,memberships,prompts):
    rows=[]
    for session in sessions:
        slug=str(session["slug"]); topics=sorted(memberships.get(slug) or {"untagged"})
        for topic in topics:
            for thread in session["_threads"]:
                status=thread["status"]
                rows.append({"topic":topic,"goal":thread["goal"],"status":status,
                    "state":thread["state"],"status_sources":[status],
                    "last_touched":str(session.get("date","") or ""),
                    "slug":slug,"pointers":pointers(session,prompts),
                    **({"open_thread_count":thread["open_thread_count"]}
                       if "open_thread_count" in thread else {})})
    snapshot=[(r["state"],r["status"],r["slug"],r["goal"]) for r in rows]
    for index,row in enumerate(rows):
        state,status,slug,goal=snapshot[index]
        if state not in {"met","open"}: continue
        opposite="open" if state=="met" else "met"
        conflicts=[]
        for other_state,other_status,other_slug,other_goal in snapshot:
            if other_slug==slug or other_state!=opposite: continue
            if overlap(goal,other_goal)>=0.6:
                conflicts.extend([status,other_status])
        if conflicts:
            row["status_sources"]=sorted(set(conflicts))
            row["status"]="conflict"; row["state"]="conflict"
    return rows

def esc(value):
    return str(value or "").replace("|","\\|").replace("\n"," ")

def display_status(row):
    if row["status"]=="conflict":
        return "conflict ("+"; ".join(row["status_sources"])+")"
    return row["status"]

def _write_atomic(dest,text,newline):
    # atomic replace: build never leaves a half-written THREADS.md/threads.jsonl for a
    # concurrent reader (checker/sibling). newline=None keeps the platform default (as
    # the old write_text did); "\n" preserves the jsonl's explicit LF.
    dest=Path(dest); dest.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=str(dest.parent),prefix=".tmp-",suffix="-"+dest.name); os.close(fd)
    try:
        with open(tmp,"w",encoding="utf-8",newline=newline) as f: f.write(text)
        os.replace(tmp,str(dest))
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

def write_outputs(args,rows,stats):
    grouped=defaultdict(list)
    for row in rows: grouped[row["topic"]].append(row)
    unknown=sum(r["state"]=="unknown" for r in rows)
    conflicts=sum(r["state"]=="conflict" for r in rows)
    lines=["---","generated-at: "+args.now,"row-count: "+str(len(rows)),
           "catalog-sessions: "+str(stats["catalog_sessions"]),
           "catalog-source-counts: "+json.dumps(stats["catalog_source_counts"],sort_keys=True),
           "registry-entries: "+str(stats["registry_entries"]),
           "prompt-estate-files: "+str(stats["prompt_files"]),
           "unknown-count: "+str(unknown),"conflict-count: "+str(conflicts),"---","",
           "# THREADS",""]
    for topic in sorted(grouped):
        lines += ["## "+topic,"",
                  "| goal | status (provenance) | last-touched | pointers |",
                  "|---|---|---|---|"]
        ordered=sorted(grouped[topic],key=lambda r:(r["goal"].lower(),r["slug"]))
        for row in ordered:
            goal=row["goal"][:160]
            lines.append("| "+esc(goal)+" | "+esc(display_status(row))+" | "+
                         esc(row["last_touched"])+" | "+esc("; ".join(row["pointers"]))+" |")
        lines.append("")
    _write_atomic(args.out,"\n".join(lines),None)
    if args.json:
        _write_atomic(args.json,
            "".join(json.dumps(row,sort_keys=True,ensure_ascii=True)+"\n" for row in rows),"\n")
    return unknown,conflicts

def run(args):
    stats=Counter(); sessions=load_catalog(args.catalog,stats); stats["catalog_sessions"]=len(sessions)
    stats["catalog_source_counts"]=dict(Counter(
        str(s.get("entry_source",s.get("source","")) or "UNKNOWN") for s in sessions))
    memberships=registry_memberships(args.chests,sessions,stats)
    prompts=prompt_map(args.prompt_estate,stats)
    rows=build_rows(sessions,memberships,prompts)
    unknown,conflicts=write_outputs(args,rows,stats)
    skipped={key[len("skipped_"):]:value for key,value in sorted(stats.items())
             if key.startswith("skipped_") and value}
    summary={"rows":len(rows),"sessions":len(sessions),"unknown":unknown,
             "conflicts":conflicts,"skipped_by_reason":skipped}
    print("SUMMARY: "+json.dumps(summary,sort_keys=True))
    return 0,summary,rows

def self_test():
    fixtures=Path(__file__).resolve().parent/"fixtures"
    out_dir=fixtures/"_selftest"/"threads"
    args=argparse.Namespace(catalog=str(fixtures/"sessions.jsonl"),
        chests=str(fixtures/"chests"),prompt_estate=str(fixtures/"prompt-estate"),
        out=str(out_dir/"THREADS.md"),json=str(out_dir/"threads.jsonl"),
        now="2026-07-17T00:00:00-07:00")
    # pin the archive-fallback root to a fixture dir so the self-test never
    # touches (or accidentally resolves against) the real ~/.claude archive
    arch=fixtures/"plans-archive"/"2026-01-01"
    arch.mkdir(parents=True,exist_ok=True)
    (arch/"archived-plan.md").write_text("x",encoding="utf-8")
    os.environ["THREADS_PLANS_ARCHIVE"]=str(fixtures/"plans-archive")
    assertions=0
    code,summary,rows=run(args)
    markdown=Path(args.out).read_text(encoding="utf-8")
    json_rows=[json.loads(x) for x in Path(args.json).read_text(encoding="utf-8").splitlines()]
    statuses={source for row in rows for source in row["status_sources"]}
    real_claim=[r for r in rows if r["slug"]=="revisit-storage-rules"
                and r["status"]=="claimed-unknown@revisit-storage-rules"]
    real_open=[r for r in rows if r["slug"]=="revisit-storage-rules"
               and "open-threads@revisit-storage-rules" in r["status_sources"]]
    checks=[(code==0,"run failed"),(summary["sessions"]==6,"tolerant session count"),
        (summary["skipped_by_reason"].get("malformed")==1,"malformed count"),
        (summary["skipped_by_reason"].get("no_claim")==1,"no-claim count"),
        (len(rows)==len(json_rows),"json row count"),
        ("generated-at: 2026-07-17T00:00:00-07:00" in markdown,"generated-at"),
        ("catalog-source-counts:" in markdown,"source counts"),
        (any(r["topic"]=="untagged" for r in rows),"untagged missing"),
        ("claimed-partial@investigate-index-errors" in statuses,"dict verdict provenance"),
        ("claimed-unknown@minimal-missing-fields" in statuses,"legacy string provenance"),
        (sum(x.startswith("open-threads@") for x in statuses)==2,"open-thread provenance"),
        (any(r["goal"]=="Open threads: 2" and r.get("open_thread_count")==2
             for r in rows),"open-thread count fallback"),
        (len(real_claim)==1 and len(real_claim[0]["goal"])==160,
         "real status-claimed path"),
        (len(real_open)==1 and real_open[0]["goal"].startswith("Consolidate duplicate")
         and real_open[0].get("open_thread_count")==3,"real open-text path"),
        (sum(r["status"]=="conflict" for r in rows)==2,"both conflict sides"),
        (all(len(r["status_sources"])>=2 for r in rows if r["status"]=="conflict"),
         "conflict sources missing"),
        (any("prompt-estate=" in p for r in rows for p in r["pointers"]),"prompt pointer"),
        (any("transcript=" in p for r in rows for p in r["pointers"]),"real path fields"),
        ("| goal | status (provenance)" in markdown,"table missing")]
    # resolve_archive: archived basename resolves; unknown path passes through
    checks+=[(resolve_archive(r"C:\nope\archived-plan.md")==str(arch/"archived-plan.md"),
              "archive fallback resolves"),
             (resolve_archive(r"C:\nope\never-existed.md")==r"C:\nope\never-existed.md",
              "archive fallback passthrough")]
    # strict durability boundary (2026-07-24, Sol-consult): a ~/.claude/plans pointer must
    # repoint to a durable copy -- dead ones heal, ALIVE ones get archived NOW (root cause of
    # the recurring THREADS FAIL was publishing an alive-but-soon-purged plans-root path).
    fake_plans=fixtures/"_selftest"/"plans"; fake_plans.mkdir(parents=True,exist_ok=True)
    os.environ["THREADS_PLANS_ROOT"]=str(fake_plans)
    (arch/"__selftest_dead__.md").write_text("dead-durable",encoding="utf-8")
    alive=fake_plans/"__selftest_alive__.md"; alive.write_text("live-body",encoding="utf-8")
    resolved_alive=resolve_archive(str(alive))
    checks+=[(resolve_archive(str(fake_plans/"__selftest_dead__.md"))==str(arch/"__selftest_dead__.md"),
              "plans-root dead pointer heals to durable"),
             (Path(resolved_alive).is_file() and Path(resolved_alive).read_bytes()==b"live-body"
              and _canon(_archive_root()) in _canon(resolved_alive)
              and _canon(fake_plans) not in _canon(resolved_alive),
              "plans-root alive pointer archived to durable, not left ephemeral")]
    # D1 (2026-07-22): membership comes from fixtures/chests/topic-registry.json,
    # never from chest prose. planning.md still name-drops finish-report-workflow
    # as a decoy -- it must stay untagged.
    frw=[r for r in rows if r["slug"]=="finish-report-workflow"]
    inv_topics={r["topic"] for r in rows if r["slug"]=="investigate-index-errors"}
    checks+=[(bool(frw) and all(r["topic"]=="untagged" for r in frw),
              "prose mention must not tag (D1)"),
             ({"planning","archives"}<=inv_topics,"extra_topics union"),
             (not any(r["topic"]=="ghost-topic" for r in rows),"empty-slug entry leaks"),
             (all("storage" in {r["topic"] for r in rows if r["slug"]==s}
                  for s in ("consolidate-storage-rules","revisit-storage-rules")),
              "registry join (sid+slug keyed)")]
    del os.environ["THREADS_PLANS_ARCHIVE"]; os.environ.pop("THREADS_PLANS_ROOT",None)
    for junk in (arch/"__selftest_dead__.md", alive, Path(resolved_alive)):
        try: junk.unlink()
        except OSError: pass
    for ok,msg in checks:
        assertions+=1
        if not ok:
            print("SELF-TEST FAIL: "+msg)
            print("SUMMARY: "+json.dumps({"assertions":assertions,"passed":False},sort_keys=True))
            return 1
    print("SUMMARY: "+json.dumps({"assertions":assertions,"passed":True},sort_keys=True))
    print("SELF-TEST PASS: "+str(assertions)+" assertions")
    return 0

def parser():
    p=argparse.ArgumentParser(description="Build THREADS provenance ledger; no verdict inference.")
    p.add_argument("--catalog",help="sessions JSONL"); p.add_argument("--chests",help="topic markdown directory")
    p.add_argument("--prompt-estate",help="per-session markdown directory")
    p.add_argument("--out",help="THREADS.md output"); p.add_argument("--json",help="optional JSONL output")
    p.add_argument("--now",help="caller-supplied generated-at value")
    p.add_argument("--self-test",action="store_true",help="run fixture-only assertions")
    return p

def main():
    p=parser(); a=p.parse_args()
    if a.self_test: return self_test()
    missing=[name for name in ("catalog","chests","prompt_estate","out","now") if not getattr(a,name)]
    if missing: p.error("required arguments missing: "+", ".join("--"+x.replace("_","-") for x in missing))
    return run(a)[0]
if __name__=="__main__": sys.exit(main())
