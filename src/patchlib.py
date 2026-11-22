"""Unified diff parsing, applying, and a symlink overlay of the sqlglot tree."""

import os
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    def __init__(self):
        self.old = None  # path in the old tree, None if added
        self.new = None  # path in the new tree, None if deleted
        self.hunks = []  # (old_start, old_len, new_start, new_len, lines)
        self.binary = False

    @property
    def path(self):
        return self.new or self.old


def _strip(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                files.append(cur)
            cur.old = _strip(line[4:])
            cur.new = _strip(lines[i + 1][4:])
            i += 2
            continue
        if cur is not None and line.startswith("new file mode"):
            cur.old = None
        elif cur is not None and line.startswith("deleted file mode"):
            cur.new = None
        elif cur is not None and line.startswith("rename from "):
            cur.old = line[len("rename from "):]
        elif cur is not None and line.startswith("rename to "):
            cur.new = line[len("rename to "):]
        elif cur is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur.binary = True
        m = HUNK.match(line)
        if m and cur is not None:
            os_, ol, ns, nl = m.groups()
            ol = 1 if ol is None else int(ol)
            nl = 1 if nl is None else int(nl)
            os_, ns = int(os_), int(ns)
            body = []
            i += 1
            ro, rn = ol, nl
            while i < len(lines) and (ro > 0 or rn > 0):
                hl = lines[i]
                if hl.startswith("\\"):
                    i += 1
                    continue
                tag = hl[:1] if hl else " "
                if tag == " ":
                    ro -= 1
                    rn -= 1
                elif tag == "-":
                    ro -= 1
                elif tag == "+":
                    rn -= 1
                else:
                    break
                body.append((tag, hl[1:]))
                i += 1
            # "\ No newline at end of file" markers right after the hunk
            noeol_new = noeol_old = False
            while i < len(lines) and lines[i].startswith("\\"):
                prev = body[-1][0] if body else " "
                if prev in " +":
                    noeol_new = True
                if prev in " -":
                    noeol_old = True
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body, noeol_new))
            continue
        i += 1
    return files


def apply(old_text, fp):
    """Apply one file's hunks to old_text (str or None). Returns new text."""
    if fp.new is None:
        return None
    old = [] if old_text is None else old_text.split("\n")
    trailing_nl = old_text is None or old_text.endswith("\n")
    if old and old[-1] == "" and trailing_nl:
        old = old[:-1]
    out = []
    pos = 0
    noeol = False
    for os_, ol, ns, nl, body, noeol_new in fp.hunks:
        want = [t for tag, t in body if tag in " -"]
        start = os_ - 1 if ol else os_
        if old[start:start + len(want)] != want:
            # search nearby for the context
            found = None
            for d in range(1, 200):
                for s in (start - d, start + d):
                    if 0 <= s and old[s:s + len(want)] == want:
                        found = s
                        break
                if found is not None:
                    break
            if found is None:
                raise ValueError(f"hunk does not apply to {fp.path} at {os_}")
            start = found
        out.extend(old[pos:start])
        out.extend(t for tag, t in body if tag in " +")
        pos = start + len(want)
        noeol = noeol_new
    out.extend(old[pos:])
    if pos < len(old):
        noeol = not trailing_nl
    text = "\n".join(out)
    if out and not noeol:
        text += "\n"
    return text


def make_overlay(root, dest, files):
    """files: {relpath: new text or None (deleted)}. Builds a symlink farm of
    root at dest where only directories on the way to a changed file are real."""
    tree = {}
    for rel in files:
        node = tree
        parts = rel.split("/")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = None

    def build(src, dst, node):
        os.makedirs(dst, exist_ok=True)
        try:
            names = os.listdir(src)
        except FileNotFoundError:
            names = []
        for name in names:
            if name in node:
                continue
            os.symlink(os.path.join(src, name), os.path.join(dst, name))
        for name, sub in node.items():
            if sub is not None:
                build(os.path.join(src, name), os.path.join(dst, name), sub)

    build(root, dest, tree)
    for rel, text in files.items():
        p = os.path.join(dest, rel)
        if text is None:
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return dest
