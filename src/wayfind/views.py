"""The views wayfind prints, each a plain-text block sized for an agent's context.

Paths are relative to the repository root. A line ending in ``?`` is an edge
found by name only; every other edge is sure (see the package docstring).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .index import Index
from .parse import Def

UNSURE = "? = not sure: matched by name only, or a receiver that may hold more than one class"
_KINDS = ("class", "interface", "type", "module", "function", "method")


def _under(rel: str, base: str) -> bool:
    return not base or rel == base or rel.startswith(base + "/")


def repo(ix: Index, path: str, depth: int) -> str:
    base = path.strip("/")
    counts: dict[str, Counter] = defaultdict(Counter)
    docs: dict[str, str] = {}
    for rel, (_, f) in ix.files.items():
        if not _under(rel, base):
            continue
        parts = rel[len(base) :].strip("/").split("/")
        key = "/".join(parts[:depth])
        c = counts[key]
        c["files"] += 1
        for d in f.defs:
            c[d.kind] += 1
        if len(parts) <= depth:
            docs[key] = f.doc
    out = [
        f"# {base or ix.root.name}: {sum(c['files'] for c in counts.values())} files"
    ]
    for key in sorted(counts):
        c = counts[key]
        kinds = " ".join(f"{c[k]} {k}" for k in _KINDS if c[k])
        if key in docs:
            out.append(f"{key}  {kinds}" + (f"  — {docs[key]}" if docs[key] else ""))
        else:
            out.append(f"{key}/  {c['files']} files  {kinds}")
    return "\n".join(out)


def deps(ix: Index, path: str) -> str:
    base = path.strip("/")
    edges: Counter = Counter()
    for rel, (_, f) in ix.files.items():
        if not _under(rel, base):
            continue
        for name, *_ in f.refs:
            cands, sure = ix.resolve(name, rel)
            if sure and cands[0].file != rel:
                edges[(rel, cands[0].file)] += 1
    by_src: dict[str, list[str]] = defaultdict(list)
    for (a, b), n in edges.most_common():
        by_src[a].append(f"{b}({n})")
    lines = [
        f"{a} → {', '.join(bs[:12])}{' …' if len(bs) > 12 else ''}"
        for a, bs in sorted(by_src.items())
    ]
    return "\n".join(lines) or f"no sure dependencies under {base or 'the repository'}"


def outline(ix: Index, path: str) -> str:
    out = []
    for rel in sorted(r for r in ix.files if _under(r, path.strip("/"))):
        f = ix.info(rel)
        out.append(f"## {rel}" + (f"  — {f.doc}" if f.doc else ""))
        level: dict[str, int] = {}
        for d in f.defs:
            level[d.id] = level[d.parent] + 1 if d.parent in level else 0
            doc = f"  — {d.doc}" if d.doc else ""
            out.append(f"{'  ' * level[d.id]}{d.line}: {d.signature}{doc}")
    return "\n".join(out) or f"nothing indexed under {path}"


def find(ix: Index, name: str) -> str:
    return (
        "\n".join(
            f"{d.id}  {d.kind}, lines {d.line}-{d.end_line}" for d in ix.symbol(name)
        )
        or f"{name}: not found"
    )


def show(ix: Index, symbol: str) -> str:
    out = []
    for d in ix.symbol(symbol):
        src = (ix.root / d.file).read_bytes()[d.start : d.end].decode(errors="replace")
        out.append(f"## {d.id}  lines {d.line}-{d.end_line}\n{src}")
    return "\n\n".join(out) or f"{symbol}: not found"


def callers(ix: Index, symbol: str, depth: int, calls_only: bool = True) -> str:
    """Who uses symbol, level by level, grouped by file. Level 2 and beyond
    expand only through sure edges, since a name match there multiplies noise."""
    out = []
    unsure = False
    for target in ix.symbol(symbol):
        out.append(f"# {'callers' if calls_only else 'impact'} of {target.id}")
        seen = {target.id}
        frontier: list[Def] = [target]
        for level in range(1, depth + 1):
            nxt: list[Def] = []
            by_file: dict[str, list[str]] = defaultdict(list)
            for t in frontier:
                for u in ix.uses(t, calls_only):
                    if u.where in seen:
                        continue
                    seen.add(u.where)
                    name = (
                        u.where.split(":", 1)[1] if u.where in ix.defs else "<module>"
                    )
                    by_file[u.file].append(f"{name}:{u.line}{'' if u.sure else ' ?'}")
                    unsure |= not u.sure
                    if u.sure and u.where in ix.defs:
                        nxt.append(ix.defs[u.where])
            for rel in sorted(by_file):
                out.append(
                    f"{'  ' * (level - 1)}L{level} {rel}: {', '.join(by_file[rel])}"
                )
            frontier = nxt
            if not frontier:
                break
        files = {w.split(":", 1)[0] for w in seen if w != target.id}
        out.append(f"= {len(seen) - 1} uses in {len(files)} files")
    if unsure:
        out.append(UNSURE)
    return "\n".join(out) or f"{symbol}: not found"


def callees(ix: Index, symbol: str) -> str:
    """Each call once per line and target set. A call through a base or
    protocol method lists the overrides it may reach after the first."""
    out = []
    unsure = False
    for d in ix.symbol(symbol):
        out.append(f"# callees of {d.id}")
        seen = set()
        for line, cands, sure, how in ix.callees(d):
            key = tuple(c.id for c in cands)
            if key in seen:
                continue
            seen.add(key)
            unsure |= not sure
            if how == "typed" and len(cands) > 1:
                more = f" (+ {', '.join(c.id for c in cands[1:])})" + (
                    "" if sure else " ?"
                )
            elif len(cands) > 1:
                more = f" ? ({len(cands)} candidates)"
            else:
                more = "" if sure else " ?"
            out.append(f"{line}: {cands[0].id}{more}")
    if unsure:
        out.append(UNSURE)
    return "\n".join(out) or f"{symbol}: not found"


def _pct(n: int, total: int) -> str:
    return f"{n} of {total}" + (f" ({100 * n // total}%)" if total else "")


def metrics(data: dict, sort: str, top: int) -> str:
    fs, files = data["functions"], data["files"]
    if not files:
        return f"nothing indexed under {data['path'] or 'the repository'}"
    lines = sorted(f["lines"] for f in fs) or [0]
    params = sum(f["params"] for f in fs)
    out = [
        f"# metrics of {data['path'] or 'the repository'}: {len(files)} files, "
        f"{sum(f['lines'] for f in files)} lines, {len(fs)} functions, "
        f"{len(data['classes'])} classes",
        f"function lines: median {lines[len(lines) // 2]}, p90 {lines[len(lines) * 9 // 10]}, "
        f"max {lines[-1]}; complexity max {max((f['complexity'] for f in fs), default=0)}",
        f"doc: {_pct(sum(f['doc'] for f in fs), len(fs))} functions, "
        f"{_pct(sum(f['doc'] for f in files), len(files))} files",
        f"typing: {_pct(sum(f['typed'] for f in fs), params)} parameters, "
        f"{_pct(sum(f['returns'] for f in fs), len(fs))} returns; "
        f"{sum(f['anys'] for f in fs)} any; "
        f"{sum(f['unresolved'] for f in fs)} calls on a receiver of unknown type",
        f"## functions by {sort}",
    ]
    key = sort.replace("-", "_")
    for f in sorted(fs, key=lambda f: (-f[key], f["id"]))[:top]:
        out.append(
            f"{f[key]:>6}  {f['id']}:{f['line']}  "
            f"lines {f['lines']}, cx {f['complexity']}, in {f['fan_in']}, out {f['fan_out']}"
        )
    out.append("## files by lines")
    for f in sorted(files, key=lambda f: (-f["lines"], f["file"]))[:top]:
        out.append(f"{f['lines']:>6}  {f['file']}  {f['functions']} functions")
    out.append("## folders by fan-in (folders that use them)")
    for p in data["packages"][:top]:
        out.append(
            f"{p['fan_in']:>6}  {p['package']}/  out {p['fan_out']}, {p['files']} files, {p['lines']} lines"
        )
    out.append(
        "--json gives every row; every number is per definition, file or folder, with no thresholds"
    )
    return "\n".join(out)


def cycles(data: dict) -> str:
    cs = data["cycles"]
    if not cs:
        return f"no cycles between {data['level']}s under {data['path'] or 'the repository'}"
    out = [f"# {len(cs)} cycles of uses between {data['level']}s"]
    for c in cs:
        out.append(f"{c['size']} {data['level']}s: {' → '.join(c['example'])}")
        if c["size"] > len(c["example"]) - 1:
            out.append(f"  all: {', '.join(c['nodes'])}")
    return "\n".join(out)


def unused(data: dict) -> str:
    out = [
        f"# {len(data['unused'])} definitions nothing names (by name, so a string use is missed)"
    ]
    out += [
        f"{r['id']}:{r['line']}  {r['kind']}, {r['lines']} lines"
        for r in data["unused"]
    ]
    out.append(f"# {len(data['tests_only'])} named only by tests")
    out += [f"{r['id']}:{r['line']}  {r['kind']}" for r in data["tests_only"]]
    return "\n".join(out)


def _arrow(pair: list) -> str:
    a, b = pair
    return (
        str(b)
        if a is None
        else str(a)
        if b is None
        else str(a)
        if a == b
        else f"{a}→{b}"
    )


def diff(data: dict) -> str:
    out = [f"# changes since {data['base'][:12]}: {len(data['files'])} files"]
    for f in data["files"]:
        out.append(
            f"{f['file']}  lines {_arrow([f['lines_before'], f['lines_after']])}"
        )
    for r in data["definitions"]:
        out.append(
            f"{r['status']:<8} {r['id']}  lines {_arrow(r['lines'])}"
            + (
                f", cx {_arrow(r['complexity'])}"
                if r["kind"] in ("function", "method")
                else ""
            )
        )
        if r.get("callers") or r.get("callers_by_name"):
            more = r["callers_total"] - len(r["callers"])
            out.append(
                f"    used by: {', '.join(r['callers']) or 'nothing sure'}"
                + (f" +{more}" if more > 0 else "")
                + (
                    f"; {r['callers_by_name']} more by name only (?)"
                    if r["callers_by_name"]
                    else ""
                )
            )
        if r.get("still_named"):
            more = r["still_named_total"] - len(r["still_named"])
            out.append(
                f"    still named at: {', '.join(r['still_named'])}"
                + (f" +{more}" if more > 0 else "")
            )
    return "\n".join(out)
