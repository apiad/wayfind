"""The index of one repository: its files' definitions and references, cached.

The files are what ``git ls-files`` lists with a known extension, so ignored and
untracked files are never indexed, less the ``--exclude`` patterns. The cache is
one pickle per repository, keyed by its path, holding each file's (mtime, size)
and parse; a load re-parses only the files whose stamp changed, in a pool of
processes when there are many, and rewrites the cache when any did. An excluded
file keeps its cached parse, so a query with another exclusion set re-parses
nothing.

A call on a receiver (``self.queues.turn_ended()``) resolves through the
receiver's type where the source states one, in the languages with a Spec
(specs.py): a parameter or variable annotation, a constructor call assigned to
it, a class field, or, for an attribute nothing in its class types, the one
class every assignment to an attribute of that name in the repository agrees
on. A call on a base class or protocol method also reaches every override in
a subclass, since that is what may run. Only a receiver with no stated type
falls back to a name match, printed with ``?``.
"""

from __future__ import annotations

import gc
import hashlib
import os
import pickle
import re
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .parse import EXT, FAMILY, IMPLICIT_THIS, Def, FileInfo, Ref, parse_file
from .specs import SPECS

# Bump when Def, Ref or FileInfo change shape: an old cache is then ignored.
FORMAT = 6
MAX_BYTES = 1_000_000  # generated bundles and fixtures, not code to navigate
_VENDORED = ("vendor/", "node_modules/", "third_party/")
POOL_MIN = 64  # fewer files to parse than this are not worth starting processes
CLASSY = {"class", "interface", "struct", "trait", "enum", "type"}
# An attribute whose assignments across the repository name more classes than
# this tells nothing about its type (`name`, `data`).
_MAX_TYPES = 3
# A type the source states that is not a class of the repository (`Path`,
# `dict`, a literal, what an outside module returns): calls on it reach no
# definition here, and they are not a failure to resolve.
EXTERNAL = Def("<external>", "<external>", "class", "", 0, 0, 0, 0, "", "")


@dataclass
class Use:
    where: str  # the Def.id the use sits in, or the file for module level
    line: int
    sure: bool
    file: str


@dataclass
class Stats:
    files: int = 0
    parsed: int = 0
    seconds: float = 0.0
    failed: Counter = field(default_factory=Counter)  # language -> files skipped


def repo_root(start: Path) -> Path:
    out = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise ValueError(f"{start} is not inside a git repository")
    return Path(out.stdout.strip())


def cache_file(cache_dir: Path, root: Path) -> Path:
    key = hashlib.sha1(str(root).encode()).hexdigest()[:16]
    return cache_dir / f"{root.name}-{key}.v{FORMAT}.pkl"


def excluded(rel: str, patterns) -> bool:
    """A pattern is a folder or file path (`legacy`, `src/x.py`) or a glob
    (`*.min.js`, `tests/fixtures/*`), matched against the path from the root."""
    for p in patterns:
        p = p.strip("/")
        if (
            rel == p
            or rel.startswith(p + "/")
            or fnmatch(rel, p)
            or fnmatch(rel, p + "/*")
        ):
            return True
    return False


def _parse_one(job: tuple[str, str, str]):
    root, rel, lang = job
    try:
        return rel, parse_file(rel, (Path(root) / rel).read_bytes(), lang)
    except Exception:  # no grammar (offline), or a parser error
        return rel, None


class Index:
    def __init__(self, root: Path, cache_dir: Path, exclude: tuple[str, ...] = ()):
        self.root = root.resolve()
        self.cache = cache_file(cache_dir, self.root)
        self.exclude = tuple(exclude)
        self.files: dict[str, tuple[tuple[int, int], FileInfo]] = {}
        self.stats = Stats()
        self._resolved: dict[tuple, tuple[list[Def], bool]] = {}
        self._targets: dict[tuple, tuple[list[Def], bool, str]] = {}
        self._types: dict[tuple, list[Def]] = {}
        self._refs_by_name: dict[str, list[tuple[str, Ref]]] | None = None
        self._typed = False
        self.load()

    def load(self) -> None:
        t0 = time.monotonic()
        old: dict = {}
        if self.cache.exists():
            gc.disable()  # a third off a load of hundreds of thousands of tuples
            try:
                old = pickle.loads(self.cache.read_bytes())
            except Exception:  # a truncated or foreign file: rebuild
                old = {}
            finally:
                gc.enable()
        out = subprocess.run(
            ["git", "-C", str(self.root), "ls-files", "-z"],
            capture_output=True,
            check=True,
        ).stdout
        kept: dict = {}  # excluded files' cached parses, written back untouched
        todo: list[tuple[str, str, tuple[int, int]]] = []
        for rel in out.decode(errors="replace").split("\0"):
            lang = EXT.get(os.path.splitext(rel)[1])
            if not lang or any("/" + v in "/" + rel for v in _VENDORED):
                continue
            if self.exclude and excluded(rel, self.exclude):
                if rel in old:
                    kept[rel] = old[rel]
                continue
            try:
                st = (self.root / rel).stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            stamp = (st.st_mtime_ns, st.st_size)
            if rel in old and old[rel][0] == stamp:
                self.files[rel] = old[rel]
                continue
            if st.st_size > MAX_BYTES:
                continue
            todo.append((rel, lang, stamp))
        for (rel, lang, stamp), (_, info) in zip(todo, self._parse(todo), strict=True):
            if info is None:
                self.stats.failed[lang] += 1
                continue
            self.files[rel] = (stamp, info)
            self.stats.parsed += 1
        if self.stats.parsed or len(self.files) + len(kept) != len(old):
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(
                pickle.dumps(kept | self.files, protocol=pickle.HIGHEST_PROTOCOL)
            )
            os.replace(tmp, self.cache)
        self.stats.files = len(self.files)
        self.stats.seconds = round(time.monotonic() - t0, 2)
        self.defs = {d.id: d for _, f in self.files.values() for d in f.defs}
        self.by_name: dict[str, list[Def]] = defaultdict(list)
        for d in self.defs.values():
            self.by_name[d.name].append(d)

    def _parse(self, todo):
        jobs = [(str(self.root), rel, lang) for rel, lang, _ in todo]
        workers = min(os.cpu_count() or 1, 8)
        if len(jobs) >= POOL_MIN and workers > 1:
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor

            # fork: a worker needs no re-import, and never re-runs the caller's
            # script, which forkserver and spawn do.
            ctx = (
                mp.get_context("fork") if "fork" in mp.get_all_start_methods() else None
            )
            try:
                with ProcessPoolExecutor(workers, mp_context=ctx) as pool:
                    return list(pool.map(_parse_one, jobs, chunksize=16))
            except (OSError, RuntimeError):  # no processes to be had: parse here
                pass
        return [_parse_one(j) for j in jobs]

    def info(self, rel: str) -> FileInfo:
        return self.files[rel][1]

    # resolution by name -----------------------------------------------------

    def resolve(
        self, name: str, from_file: str, bare: bool = False
    ) -> tuple[list[Def], bool]:
        """The definitions a name used in from_file may mean, and whether that is sure.

        Sure: one definition in the same file, or exactly one whose module and
        name both appear in the file's imports. Otherwise the narrowest set
        found, unsure. bare: the name stands alone, so it is no method."""
        key = (name, from_file, bare)
        if key not in self._resolved:
            self._resolved[key] = self._resolve(name, from_file, bare)
        return self._resolved[key]

    def _resolve(self, name: str, from_file: str, bare: bool) -> tuple[list[Def], bool]:
        info = self.info(from_file)
        lang = info.lang
        fam = FAMILY.get(lang, lang)
        bound = info.binds.get(name)
        if (
            bound is not None
            and bound[1]
            and not any(d.file == from_file for d in self.by_name.get(name, ()))
        ):
            stem, orig = bound
            hits = [
                d
                for d in self.by_name.get(orig, ())
                if d.parent is None
                and FAMILY.get(self.info(d.file).lang, self.info(d.file).lang) == fam
                and (not stem or _module_stem(d.file) == stem)
            ]
            if hits:
                return hits, len(hits) == 1
            if stem and stem not in self._stems():
                return [], True  # imported from outside the repository
        cands = [
            d
            for d in self.by_name.get(name, ())
            if FAMILY.get(self.info(d.file).lang, self.info(d.file).lang) == fam
            and not (bare and d.kind == "method")
        ]
        if not cands:
            return [], False
        same = [d for d in cands if d.file == from_file]
        if same:
            return same, len(same) == 1
        imports = self.info(from_file).imports
        by_module = [d for d in cands if _module_named(d.file, imports)]
        named = re.search(rf"\b{re.escape(name)}\b", imports) is not None
        if named and len(by_module) == 1:
            return by_module, True
        return by_module or cands, False

    def _stems(self) -> set[str]:
        if not hasattr(self, "_stem_set"):
            self._stem_set = {_module_stem(f) for f in self.files}
        return self._stem_set

    def symbol(self, s: str) -> list[Def]:
        """`name`, `Class.method`, `path:name` or a full id, to its definitions."""
        if s in self.defs:
            return [self.defs[s]]
        if ":" in s:
            f, q = s.split(":", 1)
            return [
                d
                for d in self.defs.values()
                if d.file.endswith(f)
                and (d.id.endswith(":" + q) or d.id.endswith("." + q))
            ]
        if "." in s:
            return [
                d
                for d in self.defs.values()
                if d.id.endswith(":" + s) or d.id.endswith("." + s)
            ]
        return list(self.by_name.get(s, ()))

    def _refs_named(self, name: str):
        if self._refs_by_name is None:
            self._refs_by_name = defaultdict(list)
            for rel, (_, f) in self.files.items():
                for r in f.refs:
                    self._refs_by_name[r[0]].append((rel, r))
        return self._refs_by_name.get(name, ())

    # resolution by type -----------------------------------------------------

    def _prepare(self) -> None:
        """Members, facts and the class hierarchy, built on the first query
        that needs them: the layout views never pay for it."""
        if self._typed:
            return
        self._typed = True
        self.members: dict[str, dict[str, Def]] = defaultdict(dict)
        for d in self.defs.values():
            if d.parent is not None:
                self.members[d.parent].setdefault(d.name, d)
        self.scoped: dict[tuple[str, str], list] = defaultdict(list)
        self.attr_any: dict[str, list] = defaultdict(list)
        for rel, (_, f) in self.files.items():
            spec = SPECS.get(f.lang)
            if spec is None:
                continue
            for scope, target, value in f.facts:
                head, _, attr = target.rpartition(".")
                fact = (value, scope, rel, head)
                if head in spec.self_names:
                    cls = self._class_of(scope)
                    if cls is not None:
                        self.scoped[(cls.id, "self." + attr)].append(fact)
                    self.attr_any[attr].append(fact)
                elif head:
                    self.attr_any[attr].append(fact)
                else:
                    self.scoped[(scope or rel, target)].append(fact)
        self.bases: dict[str, list[Def]] = {}
        self.subclasses: dict[str, list[Def]] = defaultdict(list)
        self.open_classes: set[str] = set()  # with a base from outside the repository
        for rel, (_, f) in self.files.items():
            for cid, names in f.bases.items():
                c = self.defs.get(cid)
                if c is None:
                    continue
                bs = []
                for n in names:
                    found = [b for b in self._classes_named(n, rel) if b is not c]
                    if not found and n not in ("object", "Protocol", "Generic", "ABC"):
                        self.open_classes.add(cid)
                    bs += found
                self.bases[cid] = bs
                for b in bs:
                    self.subclasses[b.id].append(c)

    def _class_of(self, scope: str | None) -> Def | None:
        d = self.defs.get(scope) if scope else None
        while d is not None and d.kind not in CLASSY:
            d = self.defs.get(d.parent) if d.parent else None
        return d

    def _classes_named(self, name: str, from_file: str) -> list[Def]:
        return [d for d in self.resolve(name, from_file)[0] if d.kind in CLASSY]

    def mro(self, c: Def) -> list[Def]:
        if c is EXTERNAL:
            return []
        out, stack = [], [c]
        while stack:
            k = stack.pop(0)
            if k not in out:
                out.append(k)
                stack.extend(self.bases.get(k.id, ()))
        return out

    def _below(self, c: Def) -> list[Def]:
        if c is EXTERNAL:
            return []
        out, stack = [], list(self.subclasses.get(c.id, ()))
        while stack:
            k = stack.pop()
            if k not in out:
                out.append(k)
                stack.extend(self.subclasses.get(k.id, ()))
        return out

    def type_of(
        self, expr: str, scope: str | None, file: str, depth: int = 0
    ) -> list[Def]:
        """The classes an expression may hold, from what the source states;
        [] when it states nothing."""
        key = (expr, scope, file)
        if key not in self._types:
            self._types[key] = []  # a cycle in the facts ends here
            self._types[key] = (
                [] if depth > 12 else self._type_of(expr, scope, file, depth)
            )
        return self._types[key]

    def _type_of(
        self, expr: str, scope: str | None, file: str, depth: int
    ) -> list[Def]:
        spec = SPECS.get(self.info(file).lang)
        if spec is None:
            return []
        if expr.endswith("()"):
            head = expr[:-2]
            if head == "super":
                c = self._class_of(scope)
                return list(self.bases.get(c.id, ())) if c else []
            recv, _, name = head.rpartition(".")
            cands, sure, how = self.lookup(recv or None, name, scope, file, depth + 1)
            if sure and not cands:
                return [EXTERNAL]  # a call into an outside module or type
            out = []
            for t in cands:
                if t.kind in CLASSY:
                    out.append(t)
                elif t.rtype:
                    out += self._classes_named(t.rtype, t.file) or [EXTERNAL]
            return _unique(out)
        head, _, attr = expr.rpartition(".")
        if not head:
            if expr in spec.outside:
                return [EXTERNAL]
            if expr in spec.self_names:
                c = self._class_of(scope)
                return [c] if c else []
            s = scope
            while s:
                if (s, expr) in self.scoped:
                    return self._eval(self.scoped[(s, expr)], depth)
                d = self.defs.get(s)
                s = d.parent if d else None
            if (file, expr) in self.scoped:
                return self._eval(self.scoped[(file, expr)], depth)
            # `Foo.create()`: the receiver is a class itself
            return [d for d in self.resolve(expr, file)[0] if d.kind in CLASSY]
        owners = self.type_of(head, scope, file, depth + 1)
        for c in owners:
            for k in self.mro(c):
                facts = self.scoped.get((k.id, "self." + attr))
                if facts:
                    found = self._eval(facts, depth)
                    if found:
                        return found
        # Nothing in the owner's classes types it: the assignments to an
        # attribute of that name elsewhere, first those whose receiver is
        # one of the owners (`reg.quota = ...` for a Registry).
        facts = self.attr_any.get(attr, ())
        if not is_test(file):  # a test's fake is not what the code holds
            facts = [f for f in facts if not is_test(f[2])] or facts
        if owners:
            ids = {k.id for c in owners for k in [c, *self._below(c)]}
            mine = [
                f
                for f in facts
                if f[3]
                and f[3] not in ("self", "this")
                and any(
                    t.id in ids
                    for t in self.type_of(f[3], f[1] or None, f[2], depth + 1)
                )
            ]
            if mine:
                return self._eval(mine, depth)
        return self._eval(facts, depth)

    def _eval(self, facts, depth: int) -> list[Def]:
        out: list[Def] = []
        for value, scope, file, _ in facts[:50]:
            if value == "x:":
                out.append(EXTERNAL)
            elif value.startswith("t:"):
                out += self._classes_named(value[2:], file) or [EXTERNAL]
            else:
                out += self.type_of(value[2:], scope or None, file, depth + 1)
        out = _unique([c for c in out if c is not EXTERNAL] or out)
        return out if len(out) <= _MAX_TYPES else []

    def lookup(
        self, recv: str | None, name: str, scope: str | None, file: str, depth: int = 0
    ) -> tuple[list[Def], bool, str]:
        """What `recv.name`, or a bare `name` when recv is None, may mean from
        inside scope: (definitions, sure, how) with how one of `name` (a bare
        name or a name match), `typed` (through the receiver's type) or
        `module` (through a module imported under that name)."""
        lang = self.info(file).lang
        spec = SPECS.get(lang)
        if spec is None:
            cands, sure = self.resolve(name, file)
            if recv is None:
                inside = scope or ""
                cands = [
                    d
                    for d in cands
                    if d.kind != "method"
                    or (
                        lang in IMPLICIT_THIS
                        and d.parent
                        and inside.startswith(d.parent + ".")
                    )
                ]
            return cands, sure, "name"
        if recv is None:
            cands, sure = self.resolve(name, file, bare=True)
            return cands, sure, "name"
        self._prepare()
        bound = self.info(file).binds.get(recv) if recv else None
        if bound is not None and not (bound[1] and self.resolve(recv, file)[0]):
            stem = (
                bound[0] if not bound[1] else bound[1]
            )  # `import x` or `from . import x`
            mods = [
                d
                for d in self.by_name.get(name, ())
                if d.parent is None and _module_stem(d.file) == stem and d.file != file
            ]
            if mods:
                return mods, len(mods) == 1, "module"
            if stem not in self._stems():
                return [], True, "module"  # an outside module: nothing here
        owners = self.type_of(recv, scope, file, depth) if recv else []
        if owners:
            if all(c is EXTERNAL for c in owners):
                return [], True, "typed"
            found: list[Def] = []
            for c in owners:
                if c is EXTERNAL:
                    continue
                for k in self.mro(c):
                    m = self.members.get(k.id, {}).get(name)
                    if m is not None:
                        found.append(m)
                        break
                for k in self._below(c):
                    m = self.members.get(k.id, {}).get(name)
                    if m is not None:
                        found.append(m)
            return _unique(found), len(owners) == 1, "typed"
        fam = FAMILY.get(lang, lang)
        cands = [
            d
            for d in self.by_name.get(name, ())
            if d.parent
            is not None  # x.f() is a member: a module's f went through binds
            and FAMILY.get(self.info(d.file).lang, self.info(d.file).lang) == fam
        ]
        imports = self.info(file).imports
        near = [d for d in cands if d.file == file or _module_named(d.file, imports)]
        return near or cands, False, "name"

    def targets(self, rel: str, ref: Ref) -> tuple[list[Def], bool, str]:
        """What one reference may mean: lookup, memoized per receiver and scope."""
        name, _, _, enc, attr, recv = ref
        key = (rel, enc, recv if attr else None, name)
        if key not in self._targets:
            self._targets[key] = self.lookup(recv if attr else None, name, enc, rel)
        return self._targets[key]

    def _aliases(self, name: str) -> list[str]:
        """The local names imports give name (`import X as Y`)."""
        if not hasattr(self, "_alias_map"):
            self._alias_map: dict[str, set[str]] = defaultdict(set)
            for _, f in self.files.values():
                for local, (_, orig) in f.binds.items():
                    if orig and local != orig:
                        self._alias_map[orig].add(local)
        return sorted(self._alias_map.get(name, ()))

    def uses(self, target: Def, calls_only: bool):
        """Every reference that may mean target, as a Use."""
        refs = list(self._refs_named(target.name))
        for alias in self._aliases(target.name):
            refs += self._refs_named(alias)
        for rel, ref in refs:
            if calls_only and not ref[2]:
                continue
            cands, sure, _ = self.targets(rel, ref)
            if any(c is target for c in cands):
                yield Use(ref[3] or rel, ref[1], sure, rel)

    def callees(self, d: Def):
        """(line, candidates, sure, how) for each call made inside d."""
        for ref in self.info(d.file).refs:
            if ref[3] == d.id and ref[2]:
                cands, sure, how = self.targets(d.file, ref)
                if cands:
                    yield ref[1], cands, sure, how


_TEST = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$"
)


def is_test(rel: str) -> bool:
    return _TEST.search(rel) is not None


def _unique(defs: list[Def]) -> list[Def]:
    out: list[Def] = []
    for d in defs:
        if not any(d is o for o in out):
            out.append(d)
    return out


def _module_stem(file: str) -> str:
    p = Path(file)
    return p.parent.name if p.stem in ("__init__", "index", "mod") else p.stem


def _module_named(file: str, imports: str) -> bool:
    stem = _module_stem(file)
    return re.search(rf"\b{re.escape(stem)}\b", imports) is not None
