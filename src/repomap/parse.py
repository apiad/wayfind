"""One source file into its definitions and the names it uses.

A definition is a class, function or method with its span, signature and first
doc line. A reference is every identifier that is not a definition's name or a
binding (a parameter, an assignment target), with the definition it sits in.
Grammars come from tree-sitter-language-pack, which downloads one the first
time a language is parsed; a grammar that cannot be had raises, and the index
skips that file.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from .specs import SPECS, Spec

# Extensions to the pack's language names. Languages the pack has no tags
# query for still index, through generic_defs.
EXT = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".lua": "lua",
    ".ex": "elixir",
    ".exs": "elixir",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".sh": "bash",
    ".hs": "haskell",
    ".zig": "zig",
    ".dart": "dart",
    ".jl": "julia",
    ".ml": "ocaml",
    ".clj": "clojure",
}
# Languages whose names resolve against each other: a .ts file imports .js.
FAMILY = {"typescript": "js", "tsx": "js", "javascript": "js", "c": "c", "cpp": "c"}
# Languages where a method calls a sibling without self./this.
IMPLICIT_THIS = {"java", "csharp", "kotlin", "swift", "scala", "cpp", "ruby", "php"}

_DEF_TYPES = re.compile(
    r"(function|method|class|struct|interface|trait|enum|impl|module|constructor)"
    r"_?(definition|declaration|item|specifier)?$"
)
_CONTAINER = re.compile("class|struct|interface|trait|enum|impl|module")
_SKIP_KINDS = {"constant", "property"}
_IMPORT_LINE = re.compile(r"\s*(import|from|use|require|#include|using)\b|.*require\(")


@dataclass(slots=True)
class Def:
    id: str  # path:Qualified.name
    name: str
    kind: str
    file: str
    start: int
    end: int
    line: int
    end_line: int
    signature: str
    doc: str
    parent: str | None = None
    # Sizes and typing, for the metrics view. complexity is 1 plus the decision
    # points inside, nested functions included, as ruff's C901 counts.
    complexity: int = 0
    params: int = 0  # self and cls left out
    typed: int = 0  # of those, the ones with a type
    returns: bool = False  # the return type is stated
    anys: int = 0  # the language's "any" in the signature's types
    rtype: str = ""  # the return type's name, to type `x = f()`
    decorators: str = ""  # their names, comma-separated: a registration is a use


# A reference: (name, line, is_call, enclosing Def.id or None at module level,
# after a `.`, `->` or `::`, the receiver before it as a dotted path such as
# `self.queues` or `f()`, or "" when it is not a plain path). A tuple, not a
# class: an index holds hundreds of thousands, and tuples unpickle in a third
# of the time.
Ref = tuple[str, int, bool, str | None, bool, str]

# A stated type: (scope Def.id or "" at module level, target, value). The
# target is a name or `self.attr`; the value is `t:Type` for a type written
# down, or `e:expr` for an expression whose type the resolver works out.
Fact = tuple[str, str, str]


@dataclass
class FileInfo:
    lang: str
    doc: str
    defs: list[Def] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    imports: str = ""  # the import lines of the file's head, as text
    lines: int = 0
    facts: list[Fact] = field(default_factory=list)
    bases: dict[str, list[str]] = field(default_factory=dict)  # class id -> names
    # local name -> (module stem, name in that module, or "" for the module)
    binds: dict[str, tuple[str, str]] = field(default_factory=dict)


_queries: dict = {}


def _query(lang: str):
    import tree_sitter as ts
    import tree_sitter_language_pack as pack

    if lang not in _queries:
        src = pack.get_tags_query(lang) or ""
        if lang in ("typescript", "tsx"):
            # The TypeScript query covers only TS syntax and relies on
            # inheriting JavaScript's: alone it found 225 defs in Excalidraw,
            # combined 3,965.
            src = (pack.get_tags_query("javascript") or "") + "\n" + src
        _queries[lang] = ts.Query(pack.get_language(lang), src) if src.strip() else None
    return _queries[lang]


def first_line(s: str) -> str:
    # Drop the string's own delimiters, not a docstring's leading ``code``.
    s = re.sub(r"^[rRbBuUfF]*(\"\"\"|'''|[\"'`])|(\"\"\"|'''|[\"'`])$", "", s.strip())
    for line in s.splitlines():
        line = re.sub(r"^[\s/*#!-]+|[\s*/]+$", "", line).strip("\"' ")
        if line:
            return line[:140]
    return ""


def _py_doc(node) -> str:
    body = node if node.type == "module" else node.child_by_field_name("body")
    if body is None:
        return ""
    for c in body.named_children:
        if c.type == "comment":
            continue
        if c.type == "expression_statement" and c.named_children:
            c = c.named_children[0]
        return first_line(c.text.decode(errors="replace")) if c.type == "string" else ""
    return ""


def _comment_doc(node) -> str:
    lines: list[str] = []
    prev = node.prev_named_sibling
    while (
        prev is not None
        and "comment" in prev.type
        and node.start_point[0] - prev.end_point[0] <= 1 + len(lines)
    ):
        lines.insert(0, prev.text.decode(errors="replace"))
        prev = prev.prev_named_sibling
    return first_line("\n".join(lines))


def _file_doc(root, lang: str) -> str:
    if lang == "python":
        return _py_doc(root)
    kids = root.named_children
    if kids and "comment" in kids[0].type:
        return first_line(kids[0].text.decode(errors="replace"))
    return ""


def _signature(node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    end = (
        body.start_byte
        if body is not None
        else min(node.end_byte, node.start_byte + 200)
    )
    s = " ".join(src[node.start_byte : end].decode(errors="replace").split())
    return s.rstrip(" {:")[:160]


def _generic_defs(root):
    """For a language without a tags query: any node whose type reads like a definition."""
    stack = [root]
    while stack:
        n = stack.pop()
        if _DEF_TYPES.search(n.type):
            name = n.child_by_field_name("name")
            if name is not None:
                yield n, name, "class" if _CONTAINER.search(n.type) else "function"
        stack.extend(reversed(n.children))


def _is_binding(n) -> bool:
    par = n.parent
    if par is None:
        return False
    return (
        "parameter" in par.type
        or "keyword_argument" in par.type
        or any(par.child_by_field_name(f) == n for f in ("name", "key", "pattern"))
        or ("assignment" in par.type and par.child_by_field_name("left") == n)
    )


def parse_file(rel: str, src: bytes, lang: str) -> FileInfo:
    import tree_sitter as ts
    import tree_sitter_language_pack as pack

    root = pack.get_parser(lang).parse(src).root_node
    info = FileInfo(lang=lang, doc=_file_doc(root, lang))
    info.lines = src.count(b"\n") + (1 if src and not src.endswith(b"\n") else 0)
    spec = SPECS.get(lang)
    found = []  # (node, name node, kind)
    call_starts: set[int] = set()
    q = _query(lang)
    if q is None:
        found = list(_generic_defs(root))
    else:
        for _, caps in ts.QueryCursor(q).matches(root):
            names = caps.get("name") or []
            if not names:
                continue
            for cap, nodes in caps.items():
                if cap.startswith("definition."):
                    kind = cap.split(".", 1)[1]
                    if kind not in _SKIP_KINDS:
                        found.append((nodes[0], names[0], kind))
                elif cap.startswith("reference."):
                    call_starts.add(names[0].start_byte)
    found.sort(key=lambda f: (f[0].start_byte, -f[0].end_byte))

    by_id: dict[str, Def] = {}
    open_defs: list[Def] = []
    for node, name_node, kind in found:
        while open_defs and open_defs[-1].end <= node.start_byte:
            open_defs.pop()
        parent = open_defs[-1] if open_defs else None
        name = _text(name_node)
        if (
            kind == "function"
            and parent is not None
            and parent.kind in ("class", "interface", "module")
        ):
            kind = "method"
        qual = f"{parent.id.split(':', 1)[1]}.{name}" if parent else name
        d = Def(
            id=f"{rel}:{qual}",
            name=name,
            kind=kind,
            file=rel,
            start=node.start_byte,
            end=node.end_byte,
            line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=_signature(node, src),
            doc=_py_doc(node) if lang == "python" else _comment_doc(node),
            parent=parent.id if parent else None,
        )
        par = node.parent
        if par is not None and par.type == "decorated_definition":
            d.decorators = ",".join(
                (_dotted(c.named_children[0]) or "?").removesuffix("()")
                for c in par.named_children
                if c.type == "decorator" and c.named_children
            )
        if spec is not None and spec.jsdoc:
            jsdoc = _leading_comment(node)
            d.typed = len(_JSDOC_PARAM.findall(jsdoc))
            d.returns = _JSDOC_RETURN.search(jsdoc) is not None
        info.defs.append(d)
        open_defs.append(d)
        by_id.setdefault(d.id, d)

    starts = [d.start for d in info.defs]

    def enclosing(pos: int) -> Def | None:
        """The innermost definition whose span holds pos."""
        i = bisect_right(starts, pos) - 1
        d = info.defs[i] if i >= 0 else None
        while d is not None and not (d.start <= pos < d.end):
            d = by_id.get(d.parent) if d.parent else None
        return d

    def_names = {f[1].start_byte for f in found}
    ids, branches = _helpers(lang)
    found_ids = ts.QueryCursor(ids).captures(root).get("id", []) if ids else []
    for n in sorted(found_ids, key=lambda n: n.start_byte):  # captures come unordered
        if n.child_count or n.start_byte in def_names or _is_binding(n):
            continue
        prev = n.prev_sibling
        attr = prev is not None and prev.type in (".", "->", "::", "?.")
        recv = ""
        if attr:
            obj = n.parent.child_by_field_name("object") if n.parent else None
            if obj is None and n.parent is not None and n.parent.named_children:
                obj = n.parent.named_children[0]
            if obj is not None and obj != n:
                recv = _dotted(obj) or ""
        enc = enclosing(n.start_byte)
        info.refs.append(
            (
                src[n.start_byte : n.end_byte].decode(errors="replace"),
                n.start_point[0] + 1,
                n.start_byte in call_starts,
                enc.id if enc else None,
                attr,
                recv,
            )
        )

    # Complexity: 1 plus every decision point strictly inside the function.
    if branches is not None:
        points = sorted(
            n.start_byte
            for n in ts.QueryCursor(branches).captures(root).get("branch", ())
        )
        for d in info.defs:
            if d.kind in ("function", "method"):
                d.complexity = (
                    1 + bisect_left(points, d.end) - bisect_right(points, d.start)
                )

    if spec is not None:
        _facts(info, spec, root, enclosing)
        _binds(info, spec, root)

    head = src[:4000].decode(errors="replace")
    info.imports = "\n".join(
        line for line in head.splitlines() if _IMPORT_LINE.match(line)
    )
    return info


def _facts(info: FileInfo, spec: Spec, root, enclosing) -> None:
    import tree_sitter as ts

    q = _fact_query(info.lang, spec)
    if q is None:
        return
    for _, caps in ts.QueryCursor(q).matches(root):
        if "params" in caps:
            node = caps["params"][0]
            d = enclosing(node.start_byte)
            if d is not None and d.kind in ("function", "method"):
                _count_params(d, node, spec)
            continue
        if "rtype" in caps:
            node = caps["rtype"][0]
            d = enclosing(node.start_byte)
            if d is not None and d.kind in ("function", "method") and not d.rtype:
                d.returns = True
                d.rtype = _type_name(_text(node))
                d.anys += _count_any(node, spec)
            continue
        if "base" in caps:
            node = caps["base"][0]
            d = enclosing(node.start_byte)
            if d is not None and d.kind not in ("function", "method"):
                name = _type_name(_text(node))
                if name:
                    info.bases.setdefault(d.id, []).append(name)
            continue
        for name_cap, type_cap, prefix in (
            ("pname", "ptype", ""),
            ("fname", "ftype", "self."),
        ):
            if name_cap in caps:
                node, tnode = caps[name_cap][0], caps[type_cap][0]
                d = enclosing(node.start_byte)
                t = _type_name(_text(tnode))
                if d is not None and t:
                    name = _text(node)
                    info.facts.append((d.id, name, "t:" + t))
                    if prefix:
                        info.facts.append(
                            (d.id, f"{spec.self_names[0]}.{name}", "t:" + t)
                        )
                    else:
                        d.anys += _count_any(tnode, spec)
                break
        else:
            if "lhs" not in caps:
                continue
            lhs = caps["lhs"][0]
            target = _dotted(lhs)
            if not target:
                continue
            if "ann" in caps:
                value = _type_name(_text(caps["ann"][0]))
                value = "t:" + value if value else ""
            elif "rhs" in caps:
                value = _value(caps["rhs"][0])
            else:
                value = ""
            if not value:
                continue
            d = enclosing(lhs.start_byte)
            if (
                d is not None
                and d.kind not in ("function", "method")
                and "." not in target
                and not _in_function(lhs, d.start)
            ):
                target = f"{spec.self_names[0]}.{target}"  # a class attribute or field
            info.facts.append((d.id if d else "", target, value))


def _in_function(node, stop: int) -> bool:
    """Whether a function lies between node and the definition starting at stop:
    a TypeScript constructor is no definition of its own, so its locals would
    otherwise read as fields."""
    n = node.parent
    while n is not None and n.start_byte > stop:
        if re.search("function|method|constructor|lambda|arrow", n.type):
            return True
        n = n.parent
    return False


def _binds(info: FileInfo, spec: Spec, root) -> None:
    import tree_sitter as ts
    import tree_sitter_language_pack as pack

    key = ("imports", info.lang)
    if key not in _helpers_cache:
        _helpers_cache[key] = (
            _compile(pack.get_language(info.lang), spec.imports)
            if spec.imports
            else None
        )
    q = _helpers_cache[key]
    if q is None:
        return
    text = lambda c: _text(caps[c][0])  # noqa: E731
    for _, caps in ts.QueryCursor(q).matches(root):
        alias = text("alias") if "alias" in caps else ""
        if "name" in caps and "module" in caps:
            name = text("name").rsplit(".", 1)[-1]
            info.binds[alias or name] = (module_stem(text("module")), name)
        elif "modname" in caps:
            mod = text("modname")
            if alias or "." not in mod.strip("\"'"):
                info.binds[alias or mod] = (module_stem(mod), "")


def module_stem(module: str) -> str:
    """The last part of a module path as an import writes it: `.ops`, `a.b`,
    `"./view.js"`, `"../api/index"`; "" for a bare relative `.`."""
    m = module.strip().strip("\"'`")
    if "/" in m or m.startswith("."):
        if "/" in m:
            parts = [p for p in m.split("/") if p not in ("", ".", "..")]
            if not parts:
                return ""
            last = re.sub(r"\.(m|c)?[jt]sx?$", "", parts[-1])
            return parts[-2] if last == "index" and len(parts) > 1 else last
    return m.strip(".").rsplit(".", 1)[-1] if m.strip(".") else ""


def _text(node) -> str:
    return (node.text or b"").decode(errors="replace")


def _count_params(d: Def, node, spec: Spec) -> None:
    kids = [c for c in node.named_children if c.type != "comment"]
    if kids and kids[0].type == "identifier" and kids[0].text in (b"self", b"cls"):
        kids = kids[1:]
    params = typed = 0
    for c in kids:
        if c.type in spec.typed_params:
            params += 1
            typed += c.child_by_field_name("type") is not None
        elif c.type in spec.plain_params:
            params += 1
    d.params = params
    if spec.jsdoc:
        d.typed = min(d.typed, params)  # counted from the comment at the def
    else:
        d.typed = typed


def _count_any(node, spec: Spec) -> int:
    if not spec.any_word:
        return 0
    return len(re.findall(rf"\b{spec.any_word}\b", _text(node)))


def _type_name(text: str) -> str:
    """The class a type annotation names, or "" for a union, a generic or none.

    `X`, `X | None`, `Optional[X]`, `"X"`, `: X` and `mod.X` all give X; a
    generic gives its head (`list[X]` is a list), which matches no class."""
    s = text.strip().lstrip(":").strip().strip("\"'")
    m = re.fullmatch(r"(?:typing\.|t\.)?Optional\[(.*)\]", s)
    if m:
        s = m.group(1)
    parts = [p.strip().strip("\"'") for p in s.split("|")]
    parts = [p for p in parts if p not in ("None", "null", "undefined")]
    if len(parts) != 1:
        return ""
    m = re.match(r"[\w.]+", parts[0])
    return m.group(0).rsplit(".", 1)[-1] if m else ""


def _value(node) -> str:
    """An assigned value as a fact: a constructor's class, or an expression."""
    while node.type in ("await", "await_expression", "parenthesized_expression"):
        if not node.named_children:
            return ""
        node = node.named_children[0]
    if node.type == "new_expression":
        c = node.child_by_field_name("constructor")
        name = _dotted(c) if c is not None else None
        return "t:" + name.rsplit(".", 1)[-1] if name else ""
    if _LITERAL.search(node.type):
        return "x:"  # a value of a built-in type
    d = _dotted(node)
    return "e:" + d if d else ""


_LITERAL = re.compile(
    r"^(string|concatenated_string|template_string|integer|float|number|true|false|"
    r"list|dictionary|set|tuple|array|object|regex|"
    r"(list|dictionary|set)_comprehension|generator_expression|f_string)$"
)


_LEAF = {
    "identifier",
    "this",
    "self",
    "property_identifier",
    "field_identifier",
    "type_identifier",
}
_MEMBER = {"attribute", "member_expression", "field_expression", "selector_expression"}
_CALL = {"call", "call_expression"}


def _dotted(node, depth: int = 0) -> str | None:
    """A receiver as a dotted path (`self.a.b`, `f()`, `x.y()`), or None when
    it is anything else: a subscript, a literal, an operator."""
    if depth > 8:
        return None
    t = node.type
    if t in _LEAF:
        return _text(node)
    if t in _MEMBER:
        kids = node.named_children
        if len(kids) < 2:
            return None
        head = _dotted(kids[0], depth + 1)
        last = kids[-1]
        if head is None or last.type not in _LEAF:
            return None
        return f"{head}.{_text(last)}"
    if t in _CALL:
        f = node.child_by_field_name("function")
        head = _dotted(f, depth + 1) if f is not None else None
        return head + "()" if head else None
    return None


def _leading_comment(node) -> str:
    """The comment right above a definition or above the export around it."""
    for n in (node, node.parent):
        if n is None:
            continue
        prev = n.prev_named_sibling
        if prev is not None and "comment" in prev.type:
            return _text(prev)
    return ""


_JSDOC_PARAM = re.compile(r"@param\s*\{")
_JSDOC_RETURN = re.compile(r"@returns?\s*\{")

_BRANCH_KINDS = re.compile(
    r"^(if|elif|else_if|for|for_in|for_of|foreach|enhanced_for|range_for|while|do|"
    r"catch|except|rescue|case|switch_case|expression_case|type_case|"
    r"communication_case|match_arm|when_entry|conditional|ternary|guard)"
    r"(_statement|_clause|_expression|_arm|_entry|_block|_loop)?$"
)
_helpers_cache: dict = {}


def _helpers(lang: str):
    """The identifier query and the branch query (None: no branch kinds) of a
    language, built from its node kinds when no Spec gives the branches."""
    import tree_sitter as ts
    import tree_sitter_language_pack as pack

    if lang not in _helpers_cache:
        language = pack.get_language(lang)
        kinds: set[str] = {
            k
            for i in range(language.node_kind_count)
            if (k := language.node_kind_for_id(i))
            and language.node_kind_is_named(i)
            and language.node_kind_is_visible(i)
        }
        ident = sorted(
            k for k in kinds if k.endswith("identifier") or k in ("constant",)
        )
        ids = (
            ts.Query(language, "[" + " ".join(f"({k})" for k in ident) + "] @id")
            if ident
            else None
        )
        spec = SPECS.get(lang)
        if spec is not None and spec.branches:
            branches = _compile(language, spec.branches)
        else:
            bk = sorted(k for k in kinds if _BRANCH_KINDS.match(k))
            branches = (
                ts.Query(language, "[" + " ".join(f"({k})" for k in bk) + "] @branch")
                if bk
                else None
            )
        _helpers_cache[lang] = (ids, branches)
    return _helpers_cache[lang]


def _fact_query(lang: str, spec: Spec):
    import tree_sitter_language_pack as pack

    key = ("facts", lang)
    if key not in _helpers_cache:
        _helpers_cache[key] = _compile(pack.get_language(lang), spec.facts)
    return _helpers_cache[key]


def _compile(language, patterns: str):
    """One query from the patterns the grammar accepts, one per line."""
    import tree_sitter as ts

    ok = []
    for line in patterns.strip().splitlines():
        try:
            ts.Query(language, line)
        except Exception:  # a node or field this grammar does not have
            continue
        ok.append(line)
    return ts.Query(language, "\n".join(ok)) if ok else None
