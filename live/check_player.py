"""Play the demo fixture (a made-up session) into the live player and check the screen against the data.

Starts the server in fixture mode, reads the page four times a second and checks every
command and output, flag, search result, fetch panel, helper card, read error, message
and the final status. Saves screenshots for a visual pass.

Run: python3 live/check_player.py [--speed 2] [--shots DIR]
(plain python3: Playwright lives in the user's site-packages)
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse import SessionParser, redact  # noqa: E402
from parse_codex import CodexParser  # noqa: E402

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
FIXTURE = os.path.join(ROOT, "fixtures", "demo")
SHOW_TOP, SHOW_END = 8, 6

SNAP = """() => {
  const tx = id => { const e = document.getElementById(id); return e ? e.innerText : '' }
  return {
    term: [...document.querySelectorAll('#term-body > div')].map(e => ({c: e.className, t: e.innerText.replace(/\\u00a0/g, ' ')})),
    chrome: tx('chrome-page'), helper: tx('helper-page'), edit: tx('edit-code'), msg: tx('msg-body'),
    sub: tx('subtitle'), banner: tx('banner'), menubar: tx('menubar'), chips: tx('chips'),
    callouts: [...document.querySelectorAll('.callouts')].map(e => e.innerText).join('\\n'),
  }
}"""


def words(s):
    return re.sub(r"\s+", " ", s or "").strip()


def squash(s):
    return re.sub(r"\s+", "", s or "")


def plain(t):
    for a, b in ((r"```[\s\S]*?```", " "), (r"`([^`]+)`", r"\1"), (r"\*\*([^*]+)\*\*", r"\1"),
                 (r"\[([^\]]+)\]\([^)]+\)", r"\1"), (r"(?m)^#+\s*", ""), (r"(?m)^\s*[-*•]\s+", ""),
                 (r"(?m)^\s*\d+[.)]\s+", "")):
        t = re.sub(a, b, t)
    return words(t)


def lines(s):
    a = str(s or "").split("\n") if s else []
    while a and a[-1] == "":
        a.pop()
    return a


def term_rows(T):
    head, tail = lines(T.get("output")), lines(T.get("output_tail"))
    top, end, hidden = head, [], 0
    if tail:
        top, end = head[:SHOW_TOP], tail
        hidden = T["total_lines"] - len(top) - len(end)
    elif len(head) > SHOW_TOP + SHOW_END + 1:
        top, end = head[:SHOW_TOP], head[-SHOW_END:]
        hidden = len(head) - len(top) - len(end)
    rows = list(top) + ([f"··· {hidden} more line{'' if hidden == 1 else 's'} ···"] if hidden > 0 else []) + end
    if T.get("exit_code"):
        rows.append(T.get("error_text") or f"Exit code {T['exit_code']}")
    elif T.get("empty") and not rows:
        rows.append("(no output)")
    if T.get("harness_note"):
        rows.append(f"↳ Claude Code: {T['harness_note']}")
    return [r.rstrip() for r in rows]


def cwd_label(p):
    s = re.sub(r"^/Users/[^/]+", "~", p or "").rstrip("/")
    return "~" if s == "~" else s.split("/")[-1]


def main():
    ap = argparse.ArgumentParser()
    # 2x is still realistic. Much faster and the screen falls behind and skips ahead on purpose
    # (it says "Skipped ahead"), so some steps are never fully on screen and the checks would fail.
    ap.add_argument("--speed", type=float, default=2)
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--fixture", default=FIXTURE, help="fixtures/demo (Claude Code) or fixtures/demo-codex (Codex)")
    ap.add_argument("--shots", default=None, help="folder for screenshots every 5 s")
    ap.add_argument("--dump", default=None, help="write every screen read to this JSON file")
    args = ap.parse_args()

    fixture = os.path.abspath(args.fixture)
    log = glob.glob(os.path.join(fixture, "*.jsonl"))[0]
    parser = CodexParser(log, root=fixture) if os.path.basename(log).startswith("rollout-") else SessionParser(log)
    events = parser.poll()
    # The server redacts what it sends, so compare against the redacted data.
    steps = json.loads(redact(json.dumps(parser.steps)))
    flags = [json.loads(redact(json.dumps(e["flag"]))) for e in events if e["type"] == "flag"]
    span = (datetime.fromisoformat(parser.meta["last"].replace("Z", "+00:00"))
            - datetime.fromisoformat(parser.meta["started"].replace("Z", "+00:00"))).total_seconds()
    run_for = span / args.speed + 12

    srv = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "live", "server.py"), "--fixture", fixture,
                            "--speed", str(args.speed), "--port", str(args.port), "--no-open"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The server writes its key into a private launch page; read it the way a browser would get it.
    page = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "agentwatch",
                        f"launch-{args.port}.html")
    for _ in range(40):
        time.sleep(0.25)
        if os.path.exists(page):
            break
    key = re.search(r"#token=([A-Za-z0-9_\-]+)", open(page).read()).group(1)
    snaps, errors = [], []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1920, "height": 1080})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{args.port}/#token={key}")
            t0, next_shot = time.time(), 0.0
            while time.time() - t0 < run_for:
                snaps.append(pg.evaluate(SNAP))
                el = time.time() - t0
                if args.shots and el >= next_shot:
                    os.makedirs(args.shots, exist_ok=True)
                    pg.screenshot(path=os.path.join(args.shots, f"s{int(el):03d}.png"))
                    next_shot += 5
                time.sleep(0.25)
            b.close()
    finally:
        srv.terminate()

    if args.dump:
        json.dump(snaps, open(args.dump, "w"))
    problems, checks = [], 0

    def check(ok, what):
        nonlocal checks
        checks += 1
        if not ok:
            problems.append(what)

    for s in steps:
        k, tag = s["kind"], f"step {s['i']} {s['kind']}"
        if k == "terminal":
            T = s["terminal"]
            want_cmd = words(f"{cwd_label(T['cwd'])} % {T['command']}")
            want = term_rows(T)
            ok = False
            for sn in snaps:
                rows = sn["term"]
                for j, r in enumerate(rows):
                    if "tl" in r["c"].split() and words(r["t"]) == want_cmd:
                        got = []
                        for r2 in rows[j + 1:]:
                            if "tl" in r2["c"].split():
                                break
                            got.append(r2["t"].rstrip())
                        if got == want:
                            ok = True
                            break
                if ok:
                    break
            check(ok, f"{tag}: never shown complete: {T['command'][:70]!r}")
        elif k == "search":
            check(any(all(r["title"] in sn["chrome"] for r in s["search"]["results"]) and s["search"]["query"] in sn["chrome"]
                      for sn in snaps), f"{tag}: results page never complete")
        elif k == "fetch":
            F = s["fetch"]
            check(any(F["header"] in sn["helper"] and F["cut_note"] in sn["helper"] for sn in snaps), f"{tag}: fetch panel missing")
        elif k == "helper_start":
            first = (lines(s["helper_start"]["prompt"]) or [s["helper_start"]["description"]])[0]   # Codex logs no task text
            check(any(words(first) in words(sn["helper"]) for sn in snaps), f"{tag}: task card missing")
        elif k == "helper_end":
            first = lines(s["helper_end"]["report"])[0]
            check(any(words(first) in words(sn["helper"]) for sn in snaps), f"{tag}: report card missing")
        elif k == "read" and s["read"].get("error"):
            check(any(s["read"]["error"] in sn["edit"] for sn in snaps), f"{tag}: read error missing")
        elif k in ("read", "write"):
            # The first real line of the file, without Read's "N<tab>" prefix.
            first = re.sub(r"^\s*\d+\t", "", lines(s[k]["lines"])[0]).strip()
            check(any(first in sn["edit"] for sn in snaps), f"{tag}: file text missing ({first[:40]!r})")
        elif k == "edit":
            added = [l[1:].strip() for l in lines(s["edit"].get("patch")) if l.startswith("+") and l[1:].strip()]
            check(any(all(a in sn["edit"] for a in added) for sn in snaps), f"{tag}: added lines missing {added[:2]}")
        elif k == "prompt":
            check(any(words(lines(s["prompt"]["text"])[0]) in words(sn["banner"]) for sn in snaps), f"{tag}: prompt banner missing")
        elif k == "say":
            text = plain(s["say"]["text"])
            if len(s["say"]["text"]) > 280:
                check(any(squash(text) in squash(plain(sn["msg"])) for sn in snaps), f"{tag}: long message not in the message window")
            else:
                seen = []
                for sn in snaps:
                    sub = re.sub(r"^(Claude|Helper[^\n]*)\n", "", sn["sub"].strip())
                    if sub and (not seen or seen[-1] != sub):
                        seen.append(sub)
                check(squash(text) in squash(" ".join(seen)), f"{tag}: subtitle never spells out {text[:60]!r}")
    for f in flags:
        check(any(f["label"] in sn["callouts"] and squash(plain(f["detail"])) in squash(plain(sn["callouts"])) for sn in snaps),
              f"flag on step {f['step']} ({f['label']}) never shows with its text")
    check(bool(snaps) and "Done" in snaps[-1]["menubar"], f"final status not Done: {snaps[-1]['menubar'][-80:] if snaps else ''!r}")
    check(not errors, f"page errors: {errors[:3]}")

    print(f"{len(snaps)} screen reads, {checks} checks, {len(problems)} problems")
    for x in problems:
        print(" -", x)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
