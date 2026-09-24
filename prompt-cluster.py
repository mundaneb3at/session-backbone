#!/usr/bin/env python3
"""Mine repeated, expensive instructions from prompt-estate markdown."""
import argparse, json, re, string, sys
from collections import Counter
from pathlib import Path

NUMBERED=re.compile(r"^\s*\d+\.\s(.*)$")
SESSION=re.compile(r"^\s*session_id\s*:\s*(\S+)\s*$",re.I)
HEADING=re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
PATHS=re.compile(r"(?:[a-zA-Z]:[\\/][^\s]+|(?:https?://|file://)\S+|(?:\.?\.?[\\/])[^\s]+)")
WORDS=re.compile(r"[a-z]+")
# Greedy all-pairs membership is O(n^2); intended ceiling is about 10,000 prompts.

def normalize(text):
    text=PATHS.sub(" ",str(text or "").lower())
    text=re.sub(r"\d+"," ",text)
    text=text.translate(str.maketrans({c:" " for c in string.punctuation}))
    return " ".join(WORDS.findall(text))

def extract_file(path,stats):
    try: lines=path.read_text(encoding="utf-8",errors="replace").splitlines()
    except OSError: stats["read_errors"]+=1; return []
    session_id=path.stem
    for line in lines[:80]:
        match=SESSION.match(line)
        if match: session_id=match.group(1); break
    prompts=[]; after_prompts=False; in_grill=False; current=[]; block=[]
    def emit(parts,joiner):
        text=joiner.join(part.strip() for part in parts if part.strip()).strip()
        if text: prompts.append({"text":text,"session_id":session_id,"path":str(path)})
        else: stats["skipped"]+=1
        parts.clear()
    for line in lines:
        heading=HEADING.match(line)
        if heading:
            if current: emit(current," ")
            if block: emit(block,"\n")
            title=heading.group(2)
            if len(heading.group(1))>=2 and re.search(r"\bprompts\b",title,re.I):
                after_prompts=True
            if after_prompts: in_grill=bool(re.search(r"\bgrill\b",title,re.I))
            continue
        if not after_prompts and line.lstrip().startswith(">"):
            content=line.lstrip()[1:]
            if content.startswith(" "): content=content[1:]
            if content.strip(): block.append(content)
            elif block: emit(block,"\n")
            continue
        if block: emit(block,"\n")
        match=NUMBERED.match(line)
        if after_prompts and match:
            if current: emit(current," ")
            if not in_grill: current.append(match.group(1))
            continue
        if current: current.append(line)
    if current: emit(current," ")
    if block: emit(block,"\n")
    return prompts

def jaccard(a,b):
    if not a and not b: return 1.0
    return len(a&b)/max(1,len(a|b))

def cluster(prompts):
    groups=[]
    for prompt in prompts:
        prompt["normalized"]=normalize(prompt["text"])
        prompt["tokens"]=set(prompt["normalized"].split())
        placed=False
        for group in groups:
            if any(jaccard(prompt["tokens"],member["tokens"])>=0.55 for member in group):
                group.append(prompt); placed=True; break
        if not placed: groups.append([prompt])
    return groups

def signature(group):
    counts=Counter(token for item in group for token in item["tokens"])
    threshold=max(1,(len(group)+1)//2)
    common=sorted(token for token,count in counts.items() if count>=threshold)
    if common: return " ".join(common)
    return min((item["normalized"] for item in group),key=lambda x:(len(x),x))

def score(group):
    mean=sum(len(item["tokens"]) for item in group)/max(1,len(group))
    return len(group)*mean

def write_report(path,groups,min_cluster,total):
    eligible=[g for g in groups if len(g)>=min_cluster]
    eligible.sort(key=lambda g:(-score(g),-len(g),signature(g)))
    lines=["# Repeated Instruction Clusters",""]
    for index,group in enumerate(eligible,1):
        sessions=len({x["session_id"] for x in group})
        lines += ["## Cluster "+str(index),"",
                  "- score: "+format(score(group),".2f"),
                  "- size: "+str(len(group)),
                  "- distinct sessions: "+str(sessions),
                  "- normalized signature: "+signature(group),"",
                  "VERBATIM samples:"]
        for item in group[:3]:
            lines += ["", "- session_id: "+item["session_id"],"",
                      "  > "+item["text"].replace("\n","\n  > ")]
        lines.append("")
    clustered=sum(len(g) for g in eligible)
    pct=0.0 if not total else 100.0*(total-clustered)/total
    lines += ["UNCLUSTERED: "+format(pct,".1f")+"%",""]
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text("\n".join(lines),encoding="utf-8")
    return eligible,pct

def run(args):
    stats=Counter(); prompts=[]
    try: files=sorted(Path(args.dir).glob("*.md"),key=lambda p:p.name.lower())
    except OSError: files=[]; stats["read_errors"]+=1
    for path in files: prompts.extend(extract_file(path,stats))
    groups=cluster(prompts)
    eligible,pct=write_report(args.out,groups,args.min_cluster,len(prompts))
    summary={"files":len(files),"prompts":len(prompts),"clusters":len(eligible),
             "skipped":stats["skipped"],"read_errors":stats["read_errors"],
             "unclustered_percent":round(pct,1)}
    print("SUMMARY: "+json.dumps(summary,sort_keys=True))
    return 0,summary,eligible

def self_test():
    assertions=0
    fixtures=Path(__file__).resolve().parent/"fixtures"
    out=fixtures/"_selftest"/"cluster"/"clusters.md"
    args=argparse.Namespace(dir=str(fixtures/"prompt-estate"),out=str(out),min_cluster=3)
    code,summary,groups=run(args)
    text=out.read_text(encoding="utf-8")
    stats=Counter()
    extracted=extract_file(fixtures/"blockquote"/"block.md",stats)
    real=extract_file(fixtures/"prompt-estate"/"session-a.md",stats)
    checks=[(code==0,"run failed"),(summary["prompts"]==12,"fixture prompt count"),
        (summary["clusters"]==2,"cluster count"),(len(groups)==2,"eligible groups"),
        (all(len(g)>=3 for g in groups),"minimum size"),
        (text.count("session_id:")>=6,"samples missing"),
        ("normalized signature:" in text,"signature missing"),("VERBATIM samples:" in text,"verbatim label"),
        ("UNCLUSTERED: 33.3%" in text,"unclustered honesty"),
        (groups[0] is not groups[1],"group identity"),
        (len(extracted)==1,"blockquote extraction"),
        ("\n" in extracted[0]["text"],"blockquote join"),
        (len(real)==4,"real heading extraction"),
        ("malformed records" in real[0]["text"],"multiline prompt join"),
        (not any("grill item" in item["text"] for item in real),"grill item leaked"),
        (normalize("Use C:\\secret\\file42.md now")=="use now","path stripping")]
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
    p=argparse.ArgumentParser(description="Cluster repeated prompt instructions using token Jaccard.")
    p.add_argument("--dir",help="markdown prompt-estate directory")
    p.add_argument("--out",help="clusters markdown output")
    p.add_argument("--min-cluster",type=int,default=3,help="minimum reportable size (default 3)")
    p.add_argument("--self-test",action="store_true",help="run fixture-only assertions")
    return p

def main():
    p=parser(); a=p.parse_args()
    if a.self_test: return self_test()
    if not a.dir or not a.out: p.error("--dir and --out are required unless --self-test")
    if a.min_cluster<1: p.error("--min-cluster must be at least 1")
    return run(a)[0]
if __name__=="__main__": sys.exit(main())
