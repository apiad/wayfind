# wayfind

A tree-sitter map of a git repository: layout, outlines, callers, impact, and
numbers (metrics, cycles, unused, diff) that report and never judge. Agents use
it to read a codebase; CI reads its numbers and holds the thresholds itself.
aegis depends on it from PyPI and mounts its CLI as `aegis map`.

## What done means

- `make check` passes: format, lint, typecheck and the tests. CI runs it on
  every push and PR.
- New behaviour ships with a test that builds a small git repository and runs
  the CLI on it, as `tests/test_wayfind.py` does, watched failing first.
- A change to a command or its output updates `README.md`, which is the user
  reference. aegis's help page `code-map` describes the same commands; a change
  users of `aegis map` would notice gets a line there when aegis next bumps its
  pin.
- English everywhere.

## Where everything lives

| What | Where |
|---|---|
| Every command and what it prints | `README.md` |
| The design rules and their reasons | `DESIGN.md`, and the package docstring in `src/wayfind/__init__.py` |
| Open work | GitHub issues |
| Releases | `CHANGELOG.md`; a `vX.Y.Z` tag publishes to PyPI (`.github/workflows/release.yml`) |

## Working here

```bash
uv sync
make check
uv run wayfind repo        # the map of this repository
```

Work lands through issue, branch, PR and green CI.
