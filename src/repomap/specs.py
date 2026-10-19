"""What the map knows about one language beyond its tags query: one Spec each.

A language with no Spec still indexes (definitions, name-only calls, sizes, a
generic branch count); a Spec adds receiver types, base classes, typing counts
and an exact branch count. Adding one is filling in a Spec: the query patterns
name grammar nodes with fixed capture names, one pattern per line, and a line
the grammar cannot compile is dropped, so a pattern for a newer grammar never
breaks an older one.

Captures, each in its own pattern:

- ``@lhs`` with optional ``@ann`` (a type) and ``@rhs`` (a value): an
  assignment or a declaration. In a class body, ``x`` means ``self.x``.
- ``@pname`` with ``@ptype``: a typed parameter. ``@fname`` with ``@ftype``:
  a parameter that is also a field (TypeScript's ``constructor(private h: H)``).
- ``@base``: one base class or implemented interface of the class around it.
- ``@rtype``: the return type of the function around it.
- ``@params``: a function's parameter list, read with the kinds below.
- ``@branch``: one decision point, counted into every function around it.

The ``imports`` query binds local names: ``@module`` with ``@name`` and an
optional ``@alias`` binds a name from a module; ``@modname`` with an optional
``@alias`` binds the module itself.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    self_names: tuple[str, ...]  # what names the instance inside a method
    facts: str
    branches: str = ""  # "": the generic count over node kinds (parse.py)
    typed_params: frozenset[str] = frozenset()  # parameter kinds that carry a type
    plain_params: frozenset[str] = frozenset()  # parameter kinds that cannot
    any_word: str = ""  # the "any type", counted in signatures
    jsdoc: bool = False  # `@param {T}` in a leading comment types a parameter
    imports: str = ""
    outside: frozenset[str] = frozenset()  # global objects of the runtime


# McCabe as ruff's C901 counts it: if, elif, for, while, each except, a try's
# else, each match case, and each nested def; not boolean operators, not
# comprehensions, not lambdas.
_PY_BRANCHES = """
[(if_statement) (elif_clause) (for_statement) (while_statement) (except_clause) (case_clause) (function_definition)] @branch
(try_statement (else_clause) @branch)
"""

PYTHON = Spec(
    self_names=("self", "cls"),
    facts="""
(assignment left: (_) @lhs type: (_)? @ann right: (_)? @rhs)
(typed_parameter (identifier) @pname type: (_) @ptype)
(typed_default_parameter name: (_) @pname type: (_) @ptype)
(class_definition superclasses: (argument_list [(identifier) (attribute)] @base))
(function_definition return_type: (_) @rtype)
(function_definition parameters: (parameters) @params)
""",
    branches=_PY_BRANCHES,
    typed_params=frozenset({"typed_parameter", "typed_default_parameter"}),
    plain_params=frozenset(
        {
            "identifier",
            "default_parameter",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        }
    ),
    any_word="Any",
    imports="""
(import_from_statement module_name: (_) @module name: (dotted_name) @name)
(import_from_statement module_name: (_) @module name: (aliased_import name: (_) @name alias: (_) @alias))
(import_statement name: (dotted_name) @modname)
(import_statement name: (aliased_import name: (_) @modname alias: (_) @alias))
""",
)

# The same decision points for the C family of syntax; an arrow function is a
# lambda, so only a nested function declaration counts.
_JS_BRANCHES = """
[(if_statement) (for_statement) (for_in_statement) (while_statement) (do_statement) (switch_case) (catch_clause) (ternary_expression) (function_declaration)] @branch
"""

_JS_FACTS = """
(assignment_expression left: (_) @lhs right: (_) @rhs)
(variable_declarator name: (identifier) @lhs type: (_)? @ann value: (_)? @rhs)
(public_field_definition name: (_) @lhs type: (_)? @ann value: (_)? @rhs)
(field_definition property: (_) @lhs value: (_)? @rhs)
(required_parameter pattern: (identifier) @pname type: (_) @ptype)
(optional_parameter pattern: (identifier) @pname type: (_) @ptype)
(required_parameter (accessibility_modifier) pattern: (identifier) @fname type: (_) @ftype)
(class_heritage (extends_clause value: (_) @base))
(class_heritage (implements_clause (_) @base))
(class_heritage [(identifier) (member_expression)] @base)
(extends_type_clause type: (_) @base)
(_ return_type: (_) @rtype)
(formal_parameters) @params
"""

_JS_IMPORTS = """
(import_statement (import_clause (named_imports (import_specifier name: (_) @name alias: (_)? @alias))) source: (_) @module)
(import_statement (import_clause (namespace_import (identifier) @alias)) source: (_) @modname)
"""

_JS_GLOBALS = frozenset(
    "window document console navigator location history localStorage sessionStorage "
    "JSON Math Object Array Promise Date Number String Symbol Reflect Intl URL "
    "performance crypto indexedDB caches self globalThis customElements".split()
)

JAVASCRIPT = Spec(
    self_names=("this",),
    facts=_JS_FACTS,
    branches=_JS_BRANCHES,
    plain_params=frozenset(
        {
            "identifier",
            "assignment_pattern",
            "rest_pattern",
            "object_pattern",
            "array_pattern",
        }
    ),
    jsdoc=True,
    imports=_JS_IMPORTS,
    outside=_JS_GLOBALS,
)

TYPESCRIPT = Spec(
    self_names=("this",),
    facts=_JS_FACTS,
    branches=_JS_BRANCHES,
    typed_params=frozenset({"required_parameter", "optional_parameter"}),
    any_word="any",
    imports=_JS_IMPORTS,
    outside=_JS_GLOBALS,
)

SPECS = {
    "python": PYTHON,
    "javascript": JAVASCRIPT,
    "typescript": TYPESCRIPT,
    "tsx": TYPESCRIPT,
}
