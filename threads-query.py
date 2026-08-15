#!/usr/bin/env python3
"""Deterministically answer a work-state query from threads JSONL."""
import argparse, json, re, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

TOKEN_RE=re.compile(r"[a-z0-9]+")

def tokens(text): return set(TOKEN_RE.findall(str(text or "").lower()))

def date_key(value):
    text=str(value or "").strip()
    if not text: return float("-inf")
    try:
        parsed=datetime.fromisoformat(text.replace("Z","+00:00"))
        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError,OverflowError,OSError): return float("-inf")

def load_rows(path):
    rows=[]; malformed=0; skipped=0
    try: lines=Path(path).read_text(encoding="utf-8",errors="replace").splitlines()
    except OSError: return rows,malformed,1
    for line in lines:
        if not line.strip(): continue
        try: row=json.loads(line)
        except (json.JSONDecodeError,TypeError): malformed+=1; continue
        if not isinstance(row,dict) or not str(row.get("goal","") or "").strip():
            skipped+=1; continue
        rows.append(row)
    return rows,malformed,skipped

def rank(rows,query):
    wanted=tokens(query)
    if not wanted: return []
    hits=[]
    for row in rows:
        available=tokens(row.get("goal",""))|tokens(row.get("topic",""))
        score=len(wanted&available)/len(wanted)
        if score>0:
            hits.append((score,date_key(row.get("last_touched","")),
                         str(row.get("goal","")).lower(),row))
    hits.sort(key=lambda item:(-item[0],-item[1],item[2],
                              str(item[3].get("slug",""))))
    return hits

def esc(value): return str(value or "").replace("|","\\|").replace("\n"," ")

def status_text(row):
    status=str(row.get("status","UNKNOWN") or "UNKNOWN")
    sources=row.get("status_sources",[])
    if status=="conflict" and isinstance(sources,list) and sources:
        return "conflict ("+"; ".join(str(x) for x in sources)+")"
    return status

def fallback(query):
    safe=str(query).replace("\\","\\\\").replace('"','\\"')
    return 'FALLBACK: python brain-index/brain.py search "'+safe+'"'

def run(args):
    rows,malformed,skipped=load_rows(args.threads)
    hits=rank(rows,args.query)[:max(0,args.top)]
    if not hits:
        print("NO-MATCH")
    else:
        print("| goal | status (provenance) | last-touched | pointers |")
        print("|---|---|---|---|")
        for _,_,_,row in hits:
            pointers=row.get("pointers",[])
            if not isinstance(pointers,list): pointers=[str(pointers)]
            print("| "+esc(row.get("goal",""))+" | "+esc(status_text(row))+" | "+
                  esc(row.get("last_touched",""))+" | "+esc("; ".join(str(x) for x in pointers))+" |")
    summary={"hits":len(hits),"loaded":len(rows),"malformed":malformed,"skipped":skipped}
    print("SUMMARY: "+json.dumps(summary,sort_keys=True))
    print(fallback(args.query))
    return 0,summary,hits

def self_test():
    assertions=0
    fixtures=Path(__file__).resolve().parent/"fixtures"
    path=fixtures/"query-threads.jsonl"
    args=argparse.Namespace(threads=str(path),query="consolidate storage",top=5)
    code,summary,hits=run(args)
    no_args=argparse.Namespace(threads=str(path),query="quantum banana",top=5)
    _,no_summary,no_hits=run(no_args)
    checks=[(code==0,"run failed"),(summary["loaded"]==3,"load count"),
        (summary["malformed"]==1,"malformed count"),(len(hits)==2,"hit count"),
        (hits[0][3]["slug"]=="new-session-log","recency tiebreak"),
        (hits[0][0]==1.0,"score"),(hits[0][3]["pointers"]==["handoff=new.md"],"pointers"),
        (not no_hits,"no-match"),(no_summary["hits"]==0,"no-match summary"),
        (fallback('a "quote"').endswith('search "a \\"quote\\""'),"fallback escaping")]
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
    p=argparse.ArgumentParser(description='Answer "was X solved?" from provenance threads.')
    p.add_argument("--threads",help="threads JSONL"); p.add_argument("--query",help="query terms")
    p.add_argument("--top",type=int,default=5,help="maximum hits (default 5)")
    p.add_argument("--self-test",action="store_true",help="run fixture-only assertions")
    return p

def main():
    p=parser(); a=p.parse_args()
    if a.self_test: return self_test()
    if not a.threads or a.query is None: p.error("--threads and --query are required unless --self-test")
    return run(a)[0]
if __name__=="__main__": sys.exit(main())