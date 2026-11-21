"""Write fixtures/demo: a made-up Claude Code session in the same format Claude Code logs.

Nothing in it comes from a real person's machine. It covers every step kind the viewer
draws and every automatic flag, so tests and `server.py --fixture` can use it.

Run: python3 -I fixtures/make_demo.py
"""
import json
import os
import shutil
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "demo")
SID = "5e7d3a2c-1f4b-4c8e-9a6d-2b0c8e4f7a10"
HELPER = "agent-a1b2c3d4e5f60789"
CWD = "/home/dev/wordfreq"
T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

BUGGY = '''"""Count the most common words in a text file."""
import argparse
from collections import Counter


def top_words(text, n=10):
    """Return the n most common words as (word, count) pairs."""
    words = text.split()
    return Counter(words).most_common(n)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file")
    ap.add_argument("-n", type=int, default=10)
    args = ap.parse_args()
    with open(args.file, encoding="utf-8") as f:
        for word, count in top_words(f.read(), args.n):
            print(f"{count:>6}  {word}")


if __name__ == "__main__":
    main()
'''
TESTS = '''from wordfreq import top_words


def test_counts_simple_words():
    assert top_words("a b a c a b", 2) == [("a", 3), ("b", 2)]


def test_ignores_case():
    assert top_words("The cat. the CAT, The", 2) == [("the", 3), ("cat", 2)]


def test_strips_punctuation():
    assert top_words("Hello, world! Hello... world?", 1) == [("hello", 2)]


def test_keeps_contractions():
    assert top_words("don't stop, don't", 1) == [("don't", 2)]


def test_empty_text():
    assert top_words("", 5) == []
'''
PYTEST_FAIL = """.FF..                                                                    [100%]
=================================== FAILURES ===================================
______________________________ test_ignores_case _______________________________
>       assert top_words("The cat. the CAT, The", 2) == [("the", 3), ("cat", 2)]
E       AssertionError: assert [('The', 2), ('cat.', 1)] == [('the', 3), ('cat', 2)]
___________________________ test_strips_punctuation ____________________________
>       assert top_words("Hello, world! Hello... world?", 1) == [("hello", 2)]
E       AssertionError: assert [('Hello,', 1)] == [('hello', 2)]
=========================== short test summary info ============================
FAILED test_wordfreq.py::test_ignores_case - AssertionError: assert [('The', ...
FAILED test_wordfreq.py::test_strips_punctuation - AssertionError: assert [('...
2 failed, 3 passed in 0.05s"""
PUSH_REJECTED = """To github.example:dev/wordfreq.git
 ! [rejected]        main -> main (fetch first)
error: failed to push some refs to 'github.example:dev/wordfreq.git'"""
SEARCH_LINKS = [
    {"title": "re — Regular expression operations — Python documentation", "url": "https://docs.python.org/3/library/re.html"},
    {"title": "Regular Expression HOWTO — Python documentation", "url": "https://docs.python.org/3/howto/regex.html"},
    {"title": "collections — Counter objects", "url": "https://docs.python.org/3/library/collections.html#collections.Counter"},
    {"title": "Unicode HOWTO — Python documentation", "url": "https://docs.python.org/3/howto/unicode.html"},
]
SEARCH_SUMMARY = ("The search results point to the re module docs. \\w matches Unicode word characters, so [^\\W_]+ "
                  "matches letters and digits without underscores, and an optional group like (?:'[^\\W_]+)* keeps "
                  "contractions such as don't in one piece.")
FINAL = """Done. The word counter works and all 5 tests pass.

What changed:
- `top_words()` now lowercases the text and matches words with a regex, so punctuation no longer sticks to words and "The" and "the" count together.
- Apostrophes are allowed only inside a word, so "don't" stays whole but quote marks at the edges are dropped. Curly apostrophes count too.

The helper checked Unicode: accented words like "café" are kept whole. Its PDF source couldn't be read (the fetch returned raw PDF bytes), so that check used Python itself instead.

I couldn't push: the remote has commits this copy doesn't. Run `git pull --rebase` and then push again when you're ready."""


class Log:
    def __init__(self, prefix):
        self.rows, self.n, self.prefix = [], 0, prefix

    def ts(self, sec):
        return (T0 + timedelta(seconds=sec)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def uid(self):
        self.n += 1
        return f"{self.prefix}-0000-4000-8000-{self.n:012d}"

    def add(self, sec, rec):
        rec.setdefault("uuid", self.uid())
        rec["timestamp"] = self.ts(sec)
        rec.setdefault("sessionId", SID)
        rec.setdefault("cwd", CWD)
        self.rows.append(rec)

    def prompt(self, sec, text):
        self.add(sec, {"type": "user", "message": {"role": "user", "content": text}})

    def say(self, sec, text, end=False):
        self.add(sec, {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn" if end else "tool_use",
                                                        "content": [{"type": "text", "text": text}]}})

    def call(self, sec, tid, name, inp, done, result, is_error=False, tur=None):
        self.add(sec, {"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use",
                                                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        rec = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_error}]}}
        if tur is not None:
            rec["toolUseResult"] = tur
        self.add(done, rec)

    def bash(self, sec, tid, command, description, done, out, exit_code=0):
        if exit_code:
            self.call(sec, tid, "Bash", {"command": command, "description": description}, done,
                      f"Exit code {exit_code}\n{out}", True, f"Error: Exit code {exit_code}")
        else:
            self.call(sec, tid, "Bash", {"command": command, "description": description}, done, out or "(Bash completed with no output)",
                      False, {"stdout": out, "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False})


def numbered(text):
    return "\n".join(f"{k}\t{line}" for k, line in enumerate(text.rstrip("\n").split("\n"), 1))


def main():
    m, h = Log("00000000"), Log("00000001")  # distinct ids: the parser skips repeated ones
    m.add(0, {"type": "ai-title", "aiTitle": "Word counter with tests"})
    m.prompt(1, "Add a small command-line tool that prints the most common words in a text file, with tests. "
                "Ignore case and punctuation, but keep contractions like don't in one piece.")
    m.say(5, "I'll set up the project, write the counter and its tests, then run them.")
    m.bash(7, "tu01", "mkdir -p ~/wordfreq && cd ~/wordfreq && git init -q && python3 --version",
           "Create the project and check Python", 7.6, "Python 3.12.1")
    m.call(13, "tu02", "Write", {"file_path": f"{CWD}/wordfreq.py", "content": BUGGY}, 13.2,
           f"File created successfully at: {CWD}/wordfreq.py", False,
           {"type": "create", "filePath": f"{CWD}/wordfreq.py", "content": BUGGY, "structuredPatch": [], "originalFile": None})
    m.call(21, "tu03", "Write", {"file_path": f"{CWD}/test_wordfreq.py", "content": TESTS}, 21.2,
           f"File created successfully at: {CWD}/test_wordfreq.py", False,
           {"type": "create", "filePath": f"{CWD}/test_wordfreq.py", "content": TESTS, "structuredPatch": [], "originalFile": None})
    m.bash(27, "tu04", "python3 -m pytest -q", "Run the tests", 28.1, PYTEST_FAIL, exit_code=1)
    m.say(34, "Two tests fail: split() keeps punctuation attached and counts \"The\" and \"the\" separately. "
              "Checking the code, and how to match words while keeping apostrophes.")
    m.call(36, "tu05", "Read", {"file_path": f"{CWD}/README.md"}, 36.05, "File does not exist.", True)
    m.call(38, "tu06", "Read", {"file_path": f"{CWD}/wordfreq.py"}, 38.05, numbered(BUGGY), False,
           {"type": "text", "file": {"filePath": f"{CWD}/wordfreq.py", "content": BUGGY, "numLines": 23, "startLine": 1, "totalLines": 23}})
    m.call(40, "tu07", "WebSearch", {"query": "python regex split words keep apostrophes contractions re.findall"}, 44.2,
           f'Web search results for query: "python regex split words keep apostrophes contractions re.findall"\n\nLinks: '
           f'{json.dumps(SEARCH_LINKS)}\n\n{SEARCH_SUMMARY}', False,
           {"query": "python regex split words keep apostrophes contractions re.findall",
            "results": [{"tool_use_id": "srv01", "content": SEARCH_LINKS}, SEARCH_SUMMARY], "durationSeconds": 4.2})
    m.bash(41, "tu08", 'grep -n "TODO" *.py', "Look for leftover TODO notes", 41.3, "")

    # A helper checks Unicode handling; its PDF source turns out to be unreadable.
    m.add(47, {"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use", "content": [{
        "type": "tool_use", "id": "tu09", "name": "Agent", "input": {
            "description": "Check Unicode word handling", "subagent_type": "general-purpose",
            "prompt": "Read-only. Check whether the regex [^\\W_]+(?:['\\u2019][^\\W_]+)* keeps accented words like "
                      "'café' whole. Use the PDF at https://example.org/papers/unicode-words.pdf if it helps, "
                      "and test it in python3. Report in under 80 words."}}]}})
    pdf = ("Fetched https://example.org/papers/unicode-words.pdf (HTTP 200 OK, application/pdf, 52113 characters, truncated "
           "to the first 40000, then a model-extracted summary of the rest).\n<fetched-web-content>\n%PDF-1.7\n"
           "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n4 0 obj << /Length 2917 /Filter /FlateDecode >>\nstream\n"
           "x\u009cµZÛn\u001bÉ\u0011}ß¯èÇ\u0001\u0082éé{Ã\n"
           "[The verbatim page text stops here, 40000 of 52113 characters in; re-fetching this URL returns the same split.]\n"
           "The PDF's text could not be decoded from the bytes above.\n</fetched-web-content>")
    h.call(50, "hu01", "WebFetch", {"url": "https://example.org/papers/unicode-words.pdf", "prompt": "How do word regexes treat accents?"},
           58.5, pdf, False, {"bytes": 52113, "code": 200, "codeText": "OK", "durationMs": 8400,
                              "url": "https://example.org/papers/unicode-words.pdf", "result": pdf[:200]})
    h.say(60, "The PDF came back as raw bytes, so I'll test the regex in Python directly.")
    h.bash(62, "hu02", "python3 -c \"import re; print(re.findall(r\\\"[^\\\\W_]+(?:['\\u2019][^\\\\W_]+)*\\\", 'Café crème, don\\u2019t'))\"",
           "Test the regex on accented words", 62.2, "['Café', 'crème', 'don’t']")
    h.call(66, "hu03", "SubagentHandback", {"message": "Accented words stay whole: the regex returned ['Café', 'crème', 'don’t']. "
                                                       "The PDF couldn't be read (the fetch returned raw PDF bytes), so this "
                                                       "comes from running Python, not from the paper."},
           66.1, '{"success": true}', False, {"success": True})
    m.add(66.3, {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu09", "content": [{"type": "text", "text": "Report delivered."}], "is_error": False}]},
        "toolUseResult": {"status": "completed", "agentId": HELPER}})

    old = "import argparse\nfrom collections import Counter\n"
    new = "import argparse\nimport re\nfrom collections import Counter\n\nWORD = re.compile(r\"[^\\W_]+(?:['\\u2019][^\\W_]+)*\")\n"
    m.call(72, "tu10", "Edit", {"file_path": f"{CWD}/wordfreq.py", "old_string": old, "new_string": new, "replace_all": False}, 72.1,
           f"The file {CWD}/wordfreq.py has been updated successfully.", False,
           {"filePath": f"{CWD}/wordfreq.py", "oldString": old, "newString": new, "replaceAll": False, "userModified": False,
            "originalFile": BUGGY, "structuredPatch": [{"oldStart": 1, "oldLines": 5, "newStart": 1, "newLines": 8, "lines": [
                ' """Count the most common words in a text file."""', " import argparse", "+import re",
                " from collections import Counter", "+", "+WORD = re.compile(r\"[^\\W_]+(?:['\\u2019][^\\W_]+)*\")", " ", " "]}]})
    old2, new2 = "    words = text.split()\n", "    words = WORD.findall(text.lower())\n"
    m.call(76, "tu11", "Edit", {"file_path": f"{CWD}/wordfreq.py", "old_string": old2, "new_string": new2, "replace_all": False}, 76.1,
           f"The file {CWD}/wordfreq.py has been updated successfully.", False,
           {"filePath": f"{CWD}/wordfreq.py", "oldString": old2, "newString": new2, "replaceAll": False, "userModified": False,
            "originalFile": BUGGY, "structuredPatch": [{"oldStart": 8, "oldLines": 3, "newStart": 8, "newLines": 3, "lines": [
                '     """Return the n most common words as (word, count) pairs."""', "-    words = text.split()",
                "+    words = WORD.findall(text.lower())", "     return Counter(words).most_common(n)"]}]})
    m.bash(80, "tu12", "python3 -m pytest -q", "Run the tests again", 80.9, ".....                                                                    [100%]\n5 passed in 0.01s")
    m.call(84, "tu13", "Glob", {"pattern": "**/*.py"}, 84.05, f"{CWD}/test_wordfreq.py\n{CWD}/wordfreq.py", False,
           {"filenames": [f"{CWD}/test_wordfreq.py", f"{CWD}/wordfreq.py"], "numFiles": 2})
    m.bash(87, "tu14", "git add -A && git commit -q -m 'Count words ignoring case and punctuation' && git push origin main",
           "Commit and push", 89.0, PUSH_REJECTED, exit_code=1)
    m.bash(95, "tu15", "git push origin main", "Try the push again", 96.4, PUSH_REJECTED, exit_code=1)
    m.say(104, FINAL, end=True)
    m.add(104.5, {"type": "system", "subtype": "turn_duration", "durationMs": 103500})

    # Real logs are written as things happen, so they're in time order. Keep that true here:
    # each call and its result were added together, so sort (stable, by timestamp).
    m.rows.sort(key=lambda r: r["timestamp"])
    h.rows.sort(key=lambda r: r["timestamp"])
    shutil.rmtree(OUT, ignore_errors=True)
    sub = os.path.join(OUT, SID, "subagents")
    os.makedirs(sub)
    with open(os.path.join(OUT, SID + ".jsonl"), "w") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in m.rows))
    for r in h.rows:
        r["isSidechain"] = True
    with open(os.path.join(sub, HELPER + ".jsonl"), "w") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in h.rows))
    with open(os.path.join(sub, HELPER + ".meta.json"), "w") as f:
        json.dump({"agentType": "general-purpose", "description": "Check Unicode word handling", "toolUseId": "tu09"}, f)
    print(f"wrote {os.path.relpath(OUT)}: {len(m.rows)} main records, {len(h.rows)} helper records")


if __name__ == "__main__":
    main()
