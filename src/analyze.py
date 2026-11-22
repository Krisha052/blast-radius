"""Work out where a Python change lands: which functions' bodies, or module/class level."""

import ast
import io
import tokenize

SKIP_TOKENS = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}


def token_key(text):
    """Token stream without comments, blank lines or positions. None if it does not tokenize."""
    try:
        toks = tokenize.generate_tokens(io.StringIO(text).readline)
        return [(t.type, t.string) for t in toks if t.type not in SKIP_TOKENS]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def compiles(text, name):
    try:
        compile(text, name, "exec", dont_inherit=True)
        return True
    except BaseException:  # noqa: BLE001
        return False


class Funcs:
    """Function spans of a source file, innermost lookup by line."""

    def __init__(self, text):
        self.spans = []  # (body_start, end, def_line, col, name)
        self.classes = []  # (start, end, name)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.spans.append((node.body[0].lineno, node.end_lineno, node.lineno, node.col_offset, node.name))
            elif isinstance(node, ast.ClassDef):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                self.classes.append((start, node.end_lineno, node.name))
        self.spans.sort(key=lambda s: s[1] - s[0])

    def func_at(self, line, indent=None):
        """Innermost function whose body holds `line`."""
        for body_start, end, def_line, col, name in self.spans:
            if indent is not None and indent <= col:
                continue
            if body_start <= line <= end:
                return (def_line, body_start, end)
            # a line between the signature and the first statement (comments)
            if def_line < line < body_start and (indent is None or indent > col):
                return (def_line, body_start, end)
        return None

    def class_at(self, line):
        best = None
        for start, end, name in self.classes:
            if start <= line <= end and (best is None or end - start < best[1] - best[0]):
                best = (start, end, name)
        return best


def _indent(s):
    return len(s) - len(s.lstrip(" \t"))


def change_sites(old_text, fp):
    """For one patched .py file yields ('func', (def_line, body_start, end)) or ('module', line)."""
    funcs = Funcs(old_text)
    sites = []
    for os_, ol, ns, nl, body, _ in fp.hunks:
        old_line = os_ if ol else os_ + 1
        pending_insert = []
        prev_old = old_line - 1
        for tag, text in body:
            if tag == " ":
                if pending_insert:
                    sites.extend(_insert_sites(funcs, prev_old, old_line, pending_insert))
                    pending_insert = []
                prev_old = old_line
                old_line += 1
            elif tag == "-":
                if text.strip() and not text.strip().startswith("#"):
                    f = funcs.func_at(old_line)
                    sites.append(("func", f) if f else ("module", old_line))
                elif pending_insert or True:
                    f = funcs.func_at(old_line)
                    sites.append(("func", f) if f else ("module_soft", old_line))
                prev_old = old_line
                old_line += 1
            else:
                pending_insert.append(text)
        if pending_insert:
            sites.extend(_insert_sites(funcs, prev_old, old_line, pending_insert))
    return sites


def _insert_sites(funcs, before, after, texts):
    code = [t for t in texts if t.strip() and not t.strip().startswith("#")]
    probe = code or [t for t in texts if t.strip()] or texts
    indent = min(_indent(t) for t in probe) if probe else 0
    for line in (after, before):
        f = funcs.func_at(line, indent)
        if f and (indent > 0):
            # make sure the insertion is indented inside the function
            return [("func", f)]
    kind = "module" if code else "module_soft"
    return [(kind, after)]


def cache_builders(text):
    """Names of functions called inside cache-miss blocks (`if x is None:` /
    `if k not in cache:` whose body stores into a subscript or attribute), and
    the line spans of those blocks. Such code runs once per process, so its
    coverage names only whichever test happened to fill the cache."""
    names, spans = set(), []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return names, spans
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        miss = (isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], (ast.Is, ast.NotIn))
                and (not isinstance(t.ops[0], ast.Is) or (isinstance(t.comparators[0], ast.Constant) and t.comparators[0].value is None)))
        if not miss:
            continue
        def process_level(tg):
            # global_cache[key] = ... or cls.attr = ...; not per-instance self.x
            if isinstance(tg, ast.Subscript) and isinstance(tg.value, ast.Name):
                return tg.value.id not in ("self",)
            if isinstance(tg, ast.Attribute) and isinstance(tg.value, ast.Name):
                return tg.value.id == "cls"
            return False

        stores = any(process_level(tg)
                     for st in node.body for sub in ast.walk(st) if isinstance(sub, ast.Assign) for tg in sub.targets)
        if not stores:
            continue
        spans.append((node.lineno, node.end_lineno))
        for st in node.body:
            for sub in ast.walk(st):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name):
                        names.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        names.add(f.attr)
    names -= {"set", "get", "append", "items", "setdefault", "id", "isinstance", "issubclass", "deepcopy",
              "getattr", "dir", "lower", "super", "next", "len", "list", "dict", "tuple"}
    return names, spans
