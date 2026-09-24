#!/usr/bin/env python3
"""Defensive full-tree census with disposition completeness checking."""
import argparse, csv, json, os, re, subprocess, sys, tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

FIELDS=["path","type","size","mtime","git_tracked","top_level","disposition","reason"]
VALID={"keep-in-place","move","archive","junk-disposal","gated","excluded-by-policy"}

def norm(v):
    v=str(v or "").strip().replace("\\","/")
    while v.startswith("./"): v=v[2:]
    return v.rstrip("/") or "."

def rel(p,r):
    try: v=p.relative_to(r).as_posix()
    except (OSError,ValueError): v=os.path.relpath(str(p),str(r)).replace("\\","/")
    return v if v!="." else "."

def top(p): return "." if p=="." else p.split("/",1)[0]

def load_rules(path,w):
    out=[]
    if not path: return out
    try:
        with Path(path).open("r",encoding="utf-8-sig",newline="") as f:
            for n,row in enumerate(csv.DictReader(f),2):
                raw=row.get("pattern",""); pat=norm(raw)
                disp=str(row.get("disposition","") or "").strip()
                reason=str(row.get("reason","") or "").strip()
                if not str(raw or "").strip(): w.append(("dispositions",str(path),"missing-pattern",n))
                elif disp not in VALID: w.append(("dispositions",pat,"invalid-disposition",n))
                elif disp in {"move","archive"} and "dest=" not in reason:
                    w.append(("dispositions",pat,"missing-dest-in-reason",n))
                else: out.append((pat,disp,reason,n))
    except (OSError,csv.Error,UnicodeError) as e:
        w.append(("dispositions",str(path),"read-error: "+str(e),""))
    return out

def classify(path,is_file,rules):
    path=norm(path); found=[]
    for pat,disp,reason,n in rules:
        exact=path==pat
        if exact or (pat!="." and path.startswith(pat+"/")):
            found.append(((int(exact and is_file),len(pat),n),disp,reason))
    if not found: return "",""
    _,disp,reason=max(found,key=lambda x:x[0])
    return disp,reason

def git_files(root,w):
    try:
        p=subprocess.run(["git","-C",str(root),"ls-files","-z"],stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,check=False,timeout=120)
    except (OSError,subprocess.SubprocessError) as e:
        w.append(("git",".","git-unavailable: "+str(e),"")); return set()
    if p.returncode:
        w.append(("git",".","git-failed: "+p.stderr.decode("utf-8","replace").strip()[:500],p.returncode))
        return set()
    return {norm(x.decode("utf-8","surrogateescape")) for x in p.stdout.split(b"\0") if x}

def metadata(path,w,name):
    try:
        s=path.stat()
        return s.st_size,datetime.fromtimestamp(s.st_mtime,timezone.utc).isoformat()
    except (OSError,OverflowError,ValueError) as e:
        w.append(("stat",name,"stat-error: "+str(e),"")); return "",""

def shallow(path,w,name):
    n=0
    try:
        with os.scandir(path) as entries:
            for _ in entries: n+=1
    except OSError as e: w.append(("exclude",name,"shallow-count-error: "+str(e),""))
    return n

def parse_robo(text):
    m=re.search(r"(?im)^\s*Files\s*:\s*([0-9]+(?:[,.][0-9]{3})*)",text or "")
    if not m: return None
    try: return int(m.group(1).replace(",","").replace(".",""))
    except ValueError: return None

def scandir_file_count(path,w,name):
    total=0; pending=[os.fspath(path)]
    while pending:
        current=pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False): pending.append(entry.path)
                        else: total+=1
                    except OSError as e:
                        total+=1
                        w.append(("scandir",name,"entry-type-error: "+str(e),entry.path))
        except OSError as e:
            w.append(("scandir",name,"directory-read-error: "+str(e),current))
    return total

def robo(path,w,name):
    cmd=["robocopy",str(path),str(path),"/L","/E","/NJH","/NP","/NFL","/NDL","/BYTES"]
    try:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                         errors="replace",check=False,timeout=300)
    except (OSError,subprocess.SubprocessError) as e:
        count=scandir_file_count(path,w,name)
        w.append(("robocopy",name,"unavailable; used scandir: "+str(e),count))
        return count
    n=parse_robo(p.stdout)
    if n is not None: return n
    return scandir_file_count(path,w,name)

def walk(root,excludes,rules,tracked,w):
    rows=[]; excluded=set(excludes)
    def onerror(e): w.append(("walk",getattr(e,"filename",""),"walk-error: "+str(e),""))
    for current,dirs,files in os.walk(root,topdown=True,onerror=onerror):
        current=Path(current); name=rel(current,root)
        size,mtime=metadata(current,w,name); disp,reason=classify(name,False,rules)
        rows.append(dict(path=name,type="directory",size=size,mtime=mtime,git_tracked=0,
                         top_level=top(name),disposition=disp,reason=reason))
        kept=[]
        for child_name in sorted(dirs):
            child=current/child_name; name=rel(child,root)
            if child_name in excluded:
                size,mtime=metadata(child,w,name)
                rows.append(dict(path=name,type="excluded",size=size,mtime=mtime,git_tracked=0,
                    top_level=top(name),disposition="excluded-by-policy",
                    reason="child_count="+str(shallow(child,w,name))))
            else: kept.append(child_name)
        dirs[:]=kept
        for child_name in sorted(files):
            child=current/child_name; name=rel(child,root); size,mtime=metadata(child,w,name)
            if child_name in excluded: disp,reason,kind="excluded-by-policy","child_count=0","excluded"
            else: disp,reason=classify(name,True,rules); kind="file"
            rows.append(dict(path=name,type=kind,size=size,mtime=mtime,
                git_tracked=int(name in tracked),top_level=top(name),disposition=disp,reason=reason))
    return rows

def write_outputs(out,rows,w):
    out.mkdir(parents=True,exist_ok=True)
    with (out/"census.csv").open("w",encoding="utf-8",newline="") as f:
        x=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore"); x.writeheader(); x.writerows(rows)
    with (out/"warnings.csv").open("w",encoding="utf-8",newline="") as f:
        x=csv.writer(f); x.writerow(["source","path","warning","detail"]); x.writerows(w)
    counts=defaultdict(Counter)
    for row in rows: counts[row["top_level"]][row["disposition"] or "UNASSIGNED"]+=1
    cols=sorted({x for group in counts.values() for x in group})
    lines=["# Census Summary",""]
    if cols:
        lines+=["| top_level | "+" | ".join(cols)+" |","|---|"+"---:|"*len(cols)]
        for name in sorted(counts):
            lines.append("| "+name+" | "+" | ".join(str(counts[name][c]) for c in cols)+" |")
    lines+=["","Total rows: "+str(len(rows)),"",
            "Unassigned: "+str(sum(not row["disposition"] for row in rows)),""]
    (out/"census-summary.md").write_text("\n".join(lines),encoding="utf-8")

def run(args):
    root=Path(args.root).resolve(); out=Path(args.out).resolve(); w=[]
    if not root.is_dir():
        summary={"rows":0,"unassigned":0,"warnings":1,"exit_code":2}
        print("ERROR: root is not a readable directory: "+str(root))
        print("SUMMARY: "+json.dumps(summary,sort_keys=True)); return 2,summary
    rules=load_rules(args.dispositions,w); tracked=git_files(root,w)
    rows=walk(root,args.exclude or [],rules,tracked,w)
    counts=Counter(row["top_level"] for row in rows if row["type"]=="file")
    try: entries=sorted(root.iterdir(),key=lambda x:x.name.lower())
    except OSError as e: w.append(("walk",".","top-level-list-error: "+str(e),"")); entries=[]
    for entry in entries:
        try: is_dir=entry.is_dir()
        except OSError as e: w.append(("stat",entry.name,"is-dir-error: "+str(e),"")); continue
        if not is_dir: continue
        external=robo(entry,w,entry.name); internal=counts[entry.name]
        if external is not None and external!=internal:
            w.append(("cross-check",entry.name,
                      "file-count-mismatch python="+str(internal)+" robocopy="+str(external),
                      external-internal))
    write_outputs(out,rows,w)
    bad=[row["path"] for row in rows if not row["disposition"]]
    print("UNASSIGNED: "+str(len(bad)))
    for path in bad[:50]: print(path)
    code=int(bool(args.assert_complete and bad))
    summary={"rows":len(rows),"unassigned":len(bad),"warnings":len(w),
             "git_tracked_files":len(tracked),"exit_code":code}
    print("SUMMARY: "+json.dumps(summary,sort_keys=True)); return code,summary

def self_test():
    assertions=0
    fixtures=Path(__file__).resolve().parent/"fixtures"
    complete_out=fixtures/"_selftest"/"census-complete"
    failure_out=fixtures/"_selftest"/"census-failure"
    complete=argparse.Namespace(root=str(fixtures/"census-tree"),out=str(complete_out),
        exclude=["excluded-cache"],dispositions=str(fixtures/"dispositions-complete.csv"),
        assert_complete=True)
    code,summary=run(complete)
    with (complete_out/"census.csv").open("r",encoding="utf-8",newline="") as f:
        rows=list(csv.DictReader(f))
    ex=[r for r in rows if r["path"]=="excluded-cache"]
    alpha=[r for r in rows if r["path"]=="alpha/a.txt"]
    failure=argparse.Namespace(root=str(fixtures/"census-tree"),out=str(failure_out),
        exclude=["excluded-cache"],dispositions=str(fixtures/"dispositions.csv"),
        assert_complete=True)
    fail_code,fail_summary=run(failure)
    checks=[(code==0,"complete fixture failed"),(summary["unassigned"]==0,"complete rules"),
        ((complete_out/"census.csv").is_file(),"census.csv missing"),
        ((complete_out/"warnings.csv").is_file(),"warnings missing"),
        ((complete_out/"census-summary.md").is_file(),"summary missing"),
        (len(rows)==7,"row count"),(len(ex)==1,"excluded missing"),
        (ex[0]["type"]=="excluded","excluded type"),
        (ex[0]["disposition"]=="excluded-by-policy","excluded disposition"),
        ("child_count=2" in ex[0]["reason"],"shallow count"),
        (not any(r["path"].startswith("excluded-cache/") for r in rows),"excluded walked"),
        (alpha[0]["disposition"]=="archive","longest prefix"),
        (parse_robo(" Files : 1,234 0 0")==1234,"robo parser"),
        (parse_robo(" Files : 1.234 0 0")==1234,"localized robo parser"),
        (parse_robo("nothing") is None,"robo false positive"),
        (scandir_file_count(fixtures/"census-tree"/"alpha",[],"alpha")==2,
         "independent scandir count"),
        (fail_code==1,"assert failure exit"),
        (fail_summary["unassigned"]==1,"one unassigned")]
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
    p=argparse.ArgumentParser(description="Census every file and directory with dispositions.")
    p.add_argument("--root"); p.add_argument("--out")
    p.add_argument("--exclude",action="append",default=[])
    p.add_argument("--dispositions"); p.add_argument("--assert-complete",action="store_true")
    p.add_argument("--self-test",action="store_true",help="run fixture-only assertions")
    return p

def main():
    p=parser(); a=p.parse_args()
    if a.self_test: return self_test()
    if not a.root or not a.out: p.error("--root and --out are required unless --self-test")
    return run(a)[0]
if __name__=="__main__": sys.exit(main())
