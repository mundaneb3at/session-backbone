#!/usr/bin/env python3
"""Convert claude.ai export conversations into prompt-estate Markdown."""
import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import owner_events

SAFE_ID=re.compile(r"[^A-Za-z0-9._-]+")

def message_text(message):
    value=message.get("text")
    if isinstance(value,str) and value.strip(): return value,0
    content=message.get("content")
    if isinstance(content,list):
        parts=[]; errors=0
        for block in content:
            if not isinstance(block,dict):
                errors+=1; continue
            text=block.get("text")
            if block.get("type")=="text" and isinstance(text,str): parts.append(text)
        joined="\n".join(parts)
        if joined.strip(): return joined,errors
        return "",errors+1
    return "",1

def conversation_date(row):
    value=row.get("updated_at") or row.get("created_at")
    text=str(value or "").strip()
    try: return date.fromisoformat(text[:10])
    except (TypeError,ValueError): return None

def human_prompts(row,stats):
    messages=row.get("chat_messages")
    if not isinstance(messages,list):
        stats["parse_errors"]+=1; return []
    prompts=[]
    for message in messages:
        if not isinstance(message,dict):
            stats["parse_errors"]+=1; continue
        if str(message.get("sender","") or "").lower()!="human": continue
        text,errors=message_text(message); stats["parse_errors"]+=errors
        if text.strip(): prompts.append(text)
    return prompts

def safe_uuid(value):
    raw=str(value or "").strip()
    return SAFE_ID.sub("_",raw).strip("._")

def markdown(session_id,prompts,parse_errors):
    lines=["---","session_id: "+session_id,
           "source_jsonl: claude.ai-export","project_dir: claude.ai",
           "prompts: "+str(len(prompts)),"grill_decisions: 0",
           "parse_errors: "+str(parse_errors),"---","",
           "## Prompts sent (verbatim)","",
           "Human messages extracted from the claude.ai data export.",""]
    for index,prompt in enumerate(prompts,1):
        lines.append(str(index)+". "+prompt)
    lines.append("")
    return "\n".join(lines)

def load_export(path):
    try:
        with Path(path).open("r",encoding="utf-8-sig") as handle: data=json.load(handle)
    except (OSError,UnicodeError,json.JSONDecodeError) as error:
        return None,str(error)
    if isinstance(data,dict) and isinstance(data.get("conversations"),list):
        data=data["conversations"]
    if not isinstance(data,list): return None,"top-level JSON must be an array"
    return data,""

def run(args):
    rows,error=load_export(args.export); stats=Counter()
    if rows is None:
        summary={"conversations_total":0,"conversations_written":0,"prompts":0,
                 "parse_errors":1,"exit_code":2}
        print("ERROR: "+error)
        print("SUMMARY: "+json.dumps(summary,sort_keys=True)); return 2,summary
    stats["conversations_total"]=len(rows); out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    seen=set()
    for row in rows:
        if not isinstance(row,dict):
            stats["parse_errors"]+=1; stats["conversations_malformed"]+=1; continue
        if args.min_date:
            changed=conversation_date(row)
            if changed is None:
                stats["parse_errors"]+=1; stats["conversations_date_unknown"]+=1; continue
            if changed<args.min_date:
                stats["conversations_before_min_date"]+=1; continue
        session_id=safe_uuid(row.get("uuid"))
        if not session_id:
            stats["parse_errors"]+=1; stats["conversations_missing_uuid"]+=1; continue
        if session_id in seen:
            stats["parse_errors"]+=1; stats["conversations_duplicate_uuid"]+=1; continue
        seen.add(session_id)
        before=stats["parse_errors"]; prompts=human_prompts(row,stats)
        if not prompts:
            stats["conversations_zero_human"]+=1; continue
        path=out/(session_id+"-prompts.md")
        try:
            path.write_text(markdown(session_id,prompts,stats["parse_errors"]-before),
                            encoding="utf-8",newline="\n")
        except OSError:
            stats["write_errors"]+=1; continue
        stats["conversations_written"]+=1; stats["prompts"]+=len(prompts)
    code=int(bool(stats["write_errors"]))
    summary={key:stats[key] for key in (
        "conversations_total","conversations_written","conversations_zero_human",
        "conversations_before_min_date","conversations_date_unknown",
        "conversations_malformed","conversations_missing_uuid",
        "conversations_duplicate_uuid","prompts","parse_errors","write_errors")}
    summary["exit_code"]=code
    print("SUMMARY: "+json.dumps(summary,sort_keys=True))
    return code,summary

def self_test():
    fixtures=Path(__file__).resolve().parent/"fixtures"
    assertions=0
    with tempfile.TemporaryDirectory() as temporary:
        out=Path(temporary)/"export"
        args=argparse.Namespace(export=str(fixtures/"conversations.json"),out=str(out),
                                min_date=date(2026,7,1))
        code,summary=run(args)
        path=out/"11111111-1111-1111-1111-111111111111-prompts.md"
        text=path.read_text(encoding="utf-8") if path.is_file() else ""
        checks=[(code==0,"run failed"),(summary["conversations_total"]==3,"conversation count"),
            (summary["conversations_written"]==1,"written count"),
            (summary["conversations_zero_human"]==1,"empty conversation"),
            (summary["conversations_before_min_date"]==1,"min-date filter"),
            (summary["prompts"]==2,"human prompt count"),
            (summary["parse_errors"]==0,"unexpected parse errors"),
            (path.is_file(),"output missing"),
            ("session_id: 11111111-1111-1111-1111-111111111111" in text,"session id"),
            ("source_jsonl: claude.ai-export" in text,"source marker"),
            ("prompts: 2" in text,"frontmatter count"),
            ("## Prompts sent (verbatim)" in text,"prompt heading"),
            ("1. First human prompt.\nIt spans two lines." in text,"string message shape"),
            ("2. Second human prompt.\nWith a second text block." in text,"content-list shape"),
            ("Assistant text must not appear." not in text,"assistant message leaked")]
    for ok,message in checks:
        assertions+=1
        if not ok:
            print("SELF-TEST FAIL: "+message)
            print("SUMMARY: "+json.dumps({"assertions":assertions,"passed":False},sort_keys=True))
            return 1
    print("SUMMARY: "+json.dumps({"assertions":assertions,"passed":True},sort_keys=True))
    print("SELF-TEST PASS: "+str(assertions)+" assertions")
    return 0

def parser():
    result=argparse.ArgumentParser(description="Convert a claude.ai conversations export.")
    result.add_argument("--export",help="conversations.json export")
    result.add_argument("--out",help="prompt-estate output directory")
    result.add_argument("--min-date",help="minimum updated_at date (YYYY-MM-DD)")
    result.add_argument("--raw-jsonl",help="lossless raw record JSONL for canonical mode")
    result.add_argument("--occurrences-jsonl",help="prebuilt SourceOccurrence JSONL for canonical mode")
    result.add_argument("--stream-id",help="safe stream identifier required with --raw-jsonl")
    result.add_argument("--source-label",help="relative logical locator required with --raw-jsonl")
    result.add_argument("--canonical-out",help="new output directory for canonical sidecars")
    result.add_argument("--self-test",action="store_true",help="run fixture-only assertions")
    return result

def canonical_run(args):
    try:
        source=Path(args.raw_jsonl or args.occurrences_jsonl)
        captured=owner_events.capture_input(source)
        if args.raw_jsonl:
            occurrences=owner_events.occurrences_from_raw_bytes(
                captured.data,args.stream_id,args.source_label)
            mode="raw-jsonl"
        else:
            occurrences=owner_events.read_occurrences_bytes(captured.data)
            mode="source-occurrences"
        summary=owner_events.publish_bundle(
            occurrences,Path(args.canonical_out),input_mode=mode,captured_input=captured)
    except (OSError,UnicodeError,owner_events.ContractError) as error:
        failed={"version":"CI-PRODUCTION-STAGING-v1","result":"FAIL",
                "error":str(error),"exit_code":2}
        print("ERROR: "+str(error))
        print("SUMMARY: "+json.dumps(failed,sort_keys=True))
        return 2
    print("SUMMARY: "+json.dumps(summary,sort_keys=True))
    return 0

def main():
    arg_parser=parser(); args=arg_parser.parse_args()
    if args.self_test: return self_test()
    canonical=any((args.raw_jsonl,args.occurrences_jsonl,args.stream_id,
                   args.source_label,args.canonical_out))
    legacy=any((args.export,args.out,args.min_date))
    if canonical and legacy:
        arg_parser.error("legacy export mode and canonical mode are mutually exclusive")
    if canonical:
        if bool(args.raw_jsonl)==bool(args.occurrences_jsonl):
            arg_parser.error("canonical mode requires exactly one of --raw-jsonl or --occurrences-jsonl")
        if not args.canonical_out:
            arg_parser.error("canonical mode requires --canonical-out")
        if args.raw_jsonl and (not args.stream_id or not args.source_label):
            arg_parser.error("--raw-jsonl requires --stream-id and --source-label")
        if args.occurrences_jsonl and (args.stream_id or args.source_label):
            arg_parser.error("--stream-id/--source-label apply only to --raw-jsonl")
        return canonical_run(args)
    if not args.export or not args.out:
        arg_parser.error("--export and --out are required unless --self-test or canonical mode")
    if args.min_date:
        try: args.min_date=date.fromisoformat(args.min_date)
        except ValueError: arg_parser.error("--min-date must be YYYY-MM-DD")
    return run(args)[0]

if __name__=="__main__": sys.exit(main())
