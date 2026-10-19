"""The numbers views: metrics, cycles, unused and diff, as plain data.

Each returns a dict that ``--json`` prints as is and views.py renders as text.
None of them judges: no thresholds, no verdicts. Which numbers matter, and
where a line falls, is the caller's decision, so a CI gate or a review is a
filter over this output rather than a script of its own.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

from .index import Index, is_test
from .parse import EXT, Def, parse_file
from .specs import SPECS

FUNCS = ("function", "method")


def _under(rel: str, base: str) -> bool:
    return not base or rel == base or rel.startswith(base + "/")


def package(rel: str) -> str:
    return str(PurePosixPath(rel).parent)


def edges(ix: Index):
    """Every sure edge as (from file, from Def.id or None, to Def, is_call)."""
    for rel, (_, f) in ix.files.items():
        for ref in f.refs:
            cands, sure, _ = ix.targets(rel, ref)
            if sure and cands:
                for c in cands:
                    yield rel, ref[3], c, ref[2]


# metrics -------------------------------------------------------------------


def metrics(ix: Index, path: str = "") -> dict:
    base = path.strip("/")
    fan_in: dict[str, set] = defaultdict(set)
    fan_out: dict[str, set] = defaultdict(set)
    pkg_out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for rel, enc, c, is_call in edges(ix):
        if c.file != rel:
            a, b = package(rel), package(c.file)
            if a != b:
                pkg_out[a][b] += 1
        if is_call and enc and enc != c.id:
            fan_in[c.id].add(enc)
            fan_out[enc].add(c.id)
    unresolved: dict[str, int] = defaultdict(int)
    for rel, (_, f) in ix.files.items():
        if not _under(rel, base) or f.lang not in SPECS:
            continue
        for ref in f.refs:
            if ref[2] and ref[4] and ref[3]:
                if ix.targets(rel, ref)[2] == "name":
                    unresolved[ref[3]] += 1

    files, functions, classes = [], [], []
    for rel in sorted(ix.files):
        if not _under(rel, base):
            continue
        f = ix.info(rel)
        fs = [d for d in f.defs if d.kind in FUNCS]
        files.append(
            {
                "file": rel,
                "lang": f.lang,
                "lines": f.lines,
                "functions": len(fs),
                "classes": sum(d.kind not in FUNCS for d in f.defs),
                "doc": bool(f.doc),
                "test": is_test(rel),
            }
        )
        for d in f.defs:
            row = {
                "id": d.id,
                "file": rel,
                "name": d.name,
                "kind": d.kind,
                "line": d.line,
                "lines": d.end_line - d.line + 1,
                "doc": bool(d.doc),
            }
            if d.kind in FUNCS:
                row |= {
                    "complexity": d.complexity,
                    "params": d.params,
                    "typed": d.typed,
                    "returns": d.returns,
                    "anys": d.anys,
                    "unresolved": unresolved.get(d.id, 0),
                    "fan_in": len(fan_in.get(d.id, ())),
                    "fan_out": len(fan_out.get(d.id, ())),
                    "decorators": d.decorators,
                }
                functions.append(row)
            else:
                classes.append(row)
    pkg_in: dict[str, set] = defaultdict(set)
    for a, outs in pkg_out.items():
        for b in outs:
            pkg_in[b].add(a)
    pkgs: dict[str, dict] = {}
    for row in files:
        p = pkgs.setdefault(
            package(row["file"]),
            {"package": package(row["file"]), "files": 0, "lines": 0},
        )
        p["files"] += 1
        p["lines"] += row["lines"]
    for name, p in pkgs.items():
        p["fan_in"] = len(pkg_in.get(name, ()))
        p["fan_out"] = len(pkg_out.get(name, ()))
        p["uses"] = dict(sorted(pkg_out.get(name, {}).items(), key=lambda kv: -kv[1]))
    return {
        "root": str(ix.root),
        "path": base,
        "files": files,
        "functions": functions,
        "classes": classes,
        "packages": sorted(pkgs.values(), key=lambda p: (-p["fan_in"], p["package"])),
    }


# cycles --------------------------------------------------------------------


def cycles(ix: Index, path: str = "", level: str = "file") -> dict:
    """Strongly connected components of the graph of sure uses between files
    (or folders), each with one concrete cycle through it."""
    base = path.strip("/")
    key = package if level == "package" else (lambda r: r)
    graph: dict[str, set[str]] = defaultdict(set)
    for rel, _, c, _ in edges(ix):
        if c.file != rel and _under(rel, base) and _under(c.file, base):
            a, b = key(rel), key(c.file)
            if a != b:
                graph[a].add(b)
    comps = [sorted(c) for c in _scc(graph) if len(c) > 1]
    comps.sort(key=lambda c: (-len(c), c))
    return {
        "level": level,
        "path": base,
        "cycles": [
            {"size": len(c), "nodes": c, "example": _a_cycle(graph, c)} for c in comps
        ],
    }


def _scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative: a deep chain of files overflows recursion."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    nodes = set(graph) | {b for bs in graph.values() for b in bs}
    for start in sorted(nodes):
        if start in index:
            continue
        work = [(start, iter(sorted(graph.get(start, ()))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on.add(start)
        while work:
            v, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(graph.get(w, ())))))
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    out.append(comp)
    return out


def _a_cycle(graph: dict[str, set[str]], comp: list[str]) -> list[str]:
    """The shortest cycle through the component's first node, by BFS."""
    inside = set(comp)
    start = comp[0]
    prev: dict[str, str] = {}
    frontier = [start]
    while frontier:
        nxt = []
        for v in frontier:
            for w in sorted(graph.get(v, ())):
                if w not in inside:
                    continue
                if w == start:
                    path = [v]
                    while path[-1] != start:
                        path.append(prev[path[-1]])
                    return [*reversed(path), start]
                if w not in prev:
                    prev[w] = v
                    nxt.append(w)
        frontier = nxt
    return comp


# unused --------------------------------------------------------------------

# Decorators that do not register a function anywhere.
_PLAIN_DECORATORS = {
    "staticmethod",
    "classmethod",
    "property",
    "override",
    "typing.override",
    "functools.wraps",
    "wraps",
    "functools.cache",
    "functools.lru_cache",
    "cache",
    "lru_cache",
    "abstractmethod",
    "abc.abstractmethod",
}


def unused(ix: Index, path: str = "") -> dict:
    """Definitions no reference names, outside their own body; and those only
    tests name. By name, so a name used anywhere counts as used: it misses
    some dead code and rarely flags live code, except what is reached by a
    string (getattr, a route table, HTML)."""
    base = path.strip("/")
    ix._prepare()
    dead, tests_only = [], []
    for rel, (_, f) in sorted(ix.files.items()):
        if not _under(rel, base) or is_test(rel):
            continue
        for d in f.defs:
            if _entry(ix, d):
                continue
            where = [
                r
                for r, ref in ix._refs_named(d.name)
                if not (
                    r == d.file
                    and ref[3]
                    and (ref[3] == d.id or ref[3].startswith(d.id + "."))
                )
            ]
            row = {
                "id": d.id,
                "kind": d.kind,
                "line": d.line,
                "lines": d.end_line - d.line + 1,
            }
            if not where:
                dead.append(row)
            elif all(is_test(r) for r in where):
                tests_only.append(row)
    return {"path": base, "unused": dead, "tests_only": tests_only}


def _entry(ix: Index, d: Def) -> bool:
    """Reached without a reference by name: dunders, mains, registrations,
    and overrides, which are called through their base."""
    if d.name.startswith("__") and d.name.endswith("__"):
        return True
    if d.name in ("main", "setUp", "tearDown") or d.name.startswith("test"):
        return True
    if d.decorators and any(
        x not in _PLAIN_DECORATORS for x in d.decorators.split(",")
    ):
        return True
    js = ix.info(d.file).lang in ("javascript", "typescript", "tsx")
    if js and re.match(r"on[a-z]", d.name):
        return True  # a handler the runtime calls once it is assigned
    parent = ix.defs.get(d.parent) if d.parent else None
    if d.kind == "method" and (parent is None or parent.kind in FUNCS):
        return True  # a member of an object literal, reached by its key
    if d.kind == "method" and parent is not None:
        if parent.id in ix.open_classes:
            return True  # may override a method of an outside base class
        if any(d.name in ix.members.get(k.id, {}) for k in ix.mro(parent)[1:]):
            return True
    return False


# diff ----------------------------------------------------------------------


def default_base(root) -> str:
    """The merge-base with origin/main (what a pull request means), else with
    main, else HEAD."""
    for ref in ("origin/main", "main", "origin/master", "master"):
        out = subprocess.run(
            ["git", "-C", str(root), "merge-base", "HEAD", ref],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    return "HEAD"


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunks(root, base: str) -> dict[str, tuple[list, list]]:
    """file -> (old line ranges, new line ranges) changed between base and
    the working tree; a pure insertion or deletion is the point around it."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "-U0",
            "--no-renames",
            "--no-color",
            base,
            "--",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files: dict[str, tuple[list, list]] = {}
    cur = None
    old_path = None
    for line in out.splitlines():
        if line.startswith("--- "):
            old_path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("+++ "):
            new_path = line[6:] if line.startswith("+++ b/") else None
            cur = files.setdefault(new_path or old_path or "", ([], []))
        elif cur is not None and (m := _HUNK.match(line)):
            a, b = int(m[1]), int(m[2] or 1)
            c, d = int(m[3]), int(m[4] or 1)
            cur[0].append((a, a + b - 1) if b else (a, a + 1))
            cur[1].append((c, c + d - 1) if d else (c, c + 1))
    return files


def _touched(defs: list[Def], ranges: list) -> list[Def]:
    return [
        d for d in defs if any(lo <= d.end_line and d.line <= hi for lo, hi in ranges)
    ]


def diff(ix: Index, base: str | None = None, limit: int = 20) -> dict:
    base = base or default_base(ix.root)
    hunks = _hunks(ix.root, base)
    texts: dict[str, bytes] = {}  # a base definition's source
    head_defs: dict[str, Def] = {}
    base_defs: dict[str, Def] = {}
    touched_head: set[str] = set()
    touched_base: set[str] = set()
    file_rows = []
    for rel, (old, new) in sorted(hunks.items()):
        lang = EXT.get(PurePosixPath(rel).suffix)
        if not lang:
            continue
        src = subprocess.run(
            ["git", "-C", str(ix.root), "show", f"{base}:{rel}"], capture_output=True
        )
        before = None
        if src.returncode == 0:
            try:
                before = parse_file(rel, src.stdout, lang)
            except Exception:  # no grammar: the file's lines still count
                before = None
        after = ix.info(rel) if rel in ix.files else None
        if after is None and before is None:
            continue
        file_rows.append(
            {
                "file": rel,
                "lines_before": before.lines if before else 0,
                "lines_after": after.lines if after else 0,
            }
        )
        if before:
            for d in before.defs:
                base_defs[d.id] = d
                texts[d.id] = src.stdout[d.start : d.end]
            touched_base |= {d.id for d in _touched(before.defs, old)}
        if after:
            for d in after.defs:
                head_defs[d.id] = d
            touched_head |= {d.id for d in _touched(after.defs, new)}
    changed = touched_head | touched_base
    rows = []
    now: dict[str, bytes] = {}  # head files' bytes
    quals_now = {d.id.split(":", 1)[1] for d in ix.defs.values()}
    for did in sorted(changed):
        h, b = head_defs.get(did), base_defs.get(did)
        d = h or b
        assert d is not None  # every id came from one side or the other
        if d.kind not in FUNCS and h and b:
            continue  # a class changed through its members, which are listed
        if h and h.file not in now:
            now[h.file] = (ix.root / h.file).read_bytes()
        if h and b and texts.get(did) == now[h.file][h.start : h.end]:
            continue  # only moved, by lines added or removed above it
        status = "modified" if h and b else "added" if h else "removed"
        if status == "removed" and did.split(":", 1)[1] in quals_now:
            status = "moved"
        row: dict[str, Any] = {
            "id": did,
            "kind": d.kind,
            "status": status,
            "lines": [
                b.end_line - b.line + 1 if b else None,
                h.end_line - h.line + 1 if h else None,
            ],
            "complexity": [b.complexity if b else None, h.complexity if h else None],
        }
        if status == "modified" and h:
            outside = {
                (u.where, u.line): u.sure
                for u in ix.uses(ix.defs.get(did, h), calls_only=False)
                if u.where not in changed
            }
            sure = [f"{w}:{n}" for (w, n), ok in outside.items() if ok]
            row["callers"] = sure[:limit]
            row["callers_total"] = len(sure)
            row["callers_by_name"] = len(outside) - len(sure)
        elif status == "removed":
            left = [
                f"{ref[3] or r}:{ref[1]}"
                for r, ref in ix._refs_named(d.name)
                if (ref[3] or r) not in changed
            ]
            row["still_named"] = left[:limit]
            row["still_named_total"] = len(left)
        rows.append(row)
    order = {"removed": 0, "modified": 1, "moved": 2, "added": 3}
    rows.sort(
        key=lambda r: (order[r["status"]], -(r.get("still_named_total") or 0), r["id"])
    )
    return {"base": base, "files": file_rows, "definitions": rows}
