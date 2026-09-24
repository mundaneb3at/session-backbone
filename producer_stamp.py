#!/usr/bin/env python3
"""producer_stamp.py - shared producer-status stamping for generated views (M1,
ledger-guards-m1-m2-m5-b4 launch card, 2026-09-04).

A generated file whose producer script died mid-run keeps its old timestamp AND its old
content and reads as current. This gives every producer two primitives:

  frontmatter_block(fields, status) -> str
      Build a YAML frontmatter block ("---\\nkey: val\\n...\\nproducer-status: ok\\n---\\n")
      for the producer's SUCCESS path.

  mark_stale(path, style) -> None
      Patch ONLY the producer-status marker of an EXISTING output file in place, leaving
      the rest of the file untouched. Use on the FAILURE path - the old content stays, but
      its header can no longer be mistaken for current.

style is "frontmatter" (a "producer-status:" line inside a leading --- ... --- block,
markdown) or "html-comment" (a "<!-- producer-status: ... -->" line right after the
doctype, HTML).
"""
import re
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def frontmatter_block(fields, status="ok"):
    lines = ["---"] + [f"{k}: {v}" for k, v in fields.items()] + [f"producer-status: {status}", "---"]
    return "\n".join(lines) + "\n"


def mark_stale(path, style):
    """Patch the producer-status marker in an existing file to FAILED, in place."""
    text = open(path, encoding="utf-8").read()
    ts = now_iso()
    marker = f"FAILED (marked stale {ts})"
    if style == "frontmatter":
        if re.search(r"(?m)^producer-status:.*$", text):
            text = re.sub(r"(?m)^producer-status:.*$", f"producer-status: {marker}", text, count=1)
        else:
            m = re.match(r"(?s)^---\n(.*?)\n---\n", text)
            if m:
                text = text[: m.end(1)] + f"\nproducer-status: {marker}" + text[m.end(1) :]
            else:
                text = f"---\nproducer-status: {marker}\n---\n" + text
    elif style == "html-comment":
        if re.search(r"(?m)^<!-- producer-status:.*-->$", text):
            text = re.sub(r"(?m)^<!-- producer-status:.*-->$", f"<!-- producer-status: {marker} -->", text, count=1)
        else:
            text = re.sub(r"(?m)^(<!doctype html>\n)", rf"\1<!-- producer-status: {marker} -->\n", text, count=1)
    else:
        raise ValueError(f"unknown style: {style}")
    open(path, "w", encoding="utf-8").write(text)


def read_status(path, style):
    """Returns the producer-status value (or None) - used by tests."""
    text = open(path, encoding="utf-8").read()
    if style == "frontmatter":
        m = re.search(r"(?m)^producer-status: (.*)$", text)
    else:
        m = re.search(r"(?m)^<!-- producer-status: (.*) -->$", text)
    return m.group(1) if m else None


def _selftest():
    import tempfile

    fm = frontmatter_block({"generated-at": "X"}, status="ok")
    assert "producer-status: ok" in fm, fm
    tmp = tempfile.mktemp(suffix=".md")
    open(tmp, "w", encoding="utf-8").write(fm + "\nBODY UNCHANGED\n")
    mark_stale(tmp, "frontmatter")
    text = open(tmp, encoding="utf-8").read()
    assert "BODY UNCHANGED" in text, "mark_stale touched the body"
    assert read_status(tmp, "frontmatter").startswith("FAILED"), text

    tmp2 = tempfile.mktemp(suffix=".html")
    open(tmp2, "w", encoding="utf-8").write("<!doctype html>\n<html>BODY</html>\n")
    mark_stale(tmp2, "html-comment")
    text2 = open(tmp2, encoding="utf-8").read()
    assert "<html>BODY</html>" in text2, "mark_stale touched the body"
    assert read_status(tmp2, "html-comment").startswith("FAILED"), text2

    print("producer_stamp SELFTEST PASS (frontmatter + html-comment, body untouched)")


if __name__ == "__main__":
    _selftest()
