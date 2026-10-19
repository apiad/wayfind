# Design — repomap

The rules and their reasons are in the package docstring
(`src/repomap/__init__.py`); this file says how the modules divide the work.

```
src/repomap/parse.py     one source file into definitions, references, facts and imports
src/repomap/specs.py     per-language query patterns: receiver types, bases, branches
src/repomap/index.py     the shared, mtime-refreshed index and name resolution
src/repomap/measures.py  metrics, cycles, unused and diff over the index
src/repomap/views.py     the plain-text views
src/repomap/cli.py       one typer subcommand per view; `app` is what aegis mounts
```

The index is one pickle per repository under the user's cache directory
(`$XDG_CACHE_HOME/repomap`), refreshed by mtime and size and replaced
atomically, so every process of the user shares it. A cold index parses in a
pool of processes.

It needs no language server. An edge found by name only is printed with `?`;
deeper levels of `callers` and `impact` follow sure edges only. What a language
adds over name matching, receiver types, base classes and an exact branch
count, is one `Spec` of query patterns in `specs.py`, so a language is data,
not code.

The numbers views report and never judge. A threshold belongs to whoever reads
them: a `jq` filter, a review, or a CI linter reading `--json`.
