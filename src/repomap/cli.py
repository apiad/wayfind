"""``aegis map``: the views of the code map, one subcommand each.

Each command indexes the git repository holding ``--repo`` (by default the
shell's directory), refreshing what changed, then prints one view. It needs no
server. A line on stderr reports a re-index that parsed files, and files skipped
because their grammar could not be had.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="A map of a git repository: its layout, outlines, callers and impact.",
)

_REPO = typer.Option(
    None, "--repo", help="A path inside the repository; default: here."
)
_EXCLUDE = typer.Option(
    [],
    "--exclude",
    "-x",
    help="Leave out a folder, file or glob, from the root; repeatable.",
)


def cache_dir() -> Path:
    """Shared by every session of this user, so one index serves them all."""
    return (
        Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        / "aegis"
        / "map"
    )


def _index(repo: Path | None, exclude: list[str] | None = None):
    from ..cli import path_here
    from .index import Index, repo_root

    try:
        root = repo_root(Path(path_here(str(repo or "."))))
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    ix = Index(root, cache_dir(), tuple(exclude or ()))
    s = ix.stats
    if s.parsed:
        typer.echo(f"[indexed {s.parsed} of {s.files} files in {s.seconds}s]", err=True)
    if s.failed:
        langs = ", ".join(f"{n} {lang}" for lang, n in s.failed.most_common())
        typer.echo(f"[skipped files with no grammar: {langs}]", err=True)
    return ix


@app.command("repo")
def repo(
    path: str = typer.Argument("", help="A folder, relative to the repository root."),
    depth: int = typer.Option(1, help="Folder levels to expand."),
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Folders and files with their definition counts and first doc lines."""
    from . import views

    typer.echo(views.repo(_index(repo, exclude), path, depth))


@app.command("deps")
def deps(
    path: str = typer.Argument("", help="Only files under this folder."),
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Which files use which, through sure edges, with how many uses."""
    from . import views

    typer.echo(views.deps(_index(repo, exclude), path))


@app.command("outline")
def outline(
    path: str = typer.Argument(
        ..., help="A file or folder, relative to the repository root."
    ),
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Classes and functions: line, signature and first doc line."""
    from . import views

    typer.echo(views.outline(_index(repo, exclude), path))


@app.command("find")
def find(
    name: str = typer.Argument(..., help="A name, Class.method or path:name."),
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Where a name is defined."""
    from . import views

    typer.echo(views.find(_index(repo, exclude), name))


@app.command("show")
def show(
    symbol: str = typer.Argument(..., help="A name, Class.method or path:name."),
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """The source of one definition."""
    from . import views

    typer.echo(views.show(_index(repo, exclude), symbol))


@app.command("callers")
def callers(
    symbol: str = typer.Argument(..., help="A name, Class.method or path:name."),
    depth: int = typer.Option(1, help="Levels of callers of callers."),
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Every call of a definition, grouped by file."""
    from . import views

    typer.echo(views.callers(_index(repo, exclude), symbol, depth))


@app.command("callees")
def callees(
    symbol: str = typer.Argument(..., help="A name, Class.method or path:name."),
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """What a definition calls."""
    from . import views

    typer.echo(views.callees(_index(repo, exclude), symbol))


@app.command("impact")
def impact(
    symbol: str = typer.Argument(..., help="A name, Class.method or path:name."),
    depth: int = typer.Option(3, help="Levels of dependents to follow."),
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Every definition that uses this one, calls or not, and what uses those."""
    from . import views

    typer.echo(views.callers(_index(repo, exclude), symbol, depth, calls_only=False))


_JSON = typer.Option(False, "--json", help="Every row as JSON.")


def _emit(data: dict, text, as_json: bool) -> None:
    import json

    typer.echo(json.dumps(data, indent=1) if as_json else text(data))


@app.command("metrics")
def metrics(
    path: str = typer.Argument("", help="Only files under this folder."),
    sort: str = typer.Option(
        "lines",
        help="Rank functions by: lines, complexity, fan-in, fan-out, unresolved, params.",
    ),
    top: int = typer.Option(20, help="Rows per ranking in the text view."),
    as_json: bool = _JSON,
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Sizes, complexity, docs, typing and fan-in of every function, file and folder."""
    from . import measures, views

    if sort.replace("-", "_") not in (
        "lines",
        "complexity",
        "fan_in",
        "fan_out",
        "unresolved",
        "params",
    ):
        typer.echo(f"unknown --sort {sort}", err=True)
        raise typer.Exit(2)
    data = measures.metrics(_index(repo, exclude), path)
    _emit(data, lambda d: views.metrics(d, sort, top), as_json)


@app.command("cycles")
def cycles(
    path: str = typer.Argument("", help="Only files under this folder."),
    level: str = typer.Option("file", help="file or package (a folder)."),
    as_json: bool = _JSON,
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Cycles of uses between files or folders."""
    from . import measures, views

    _emit(measures.cycles(_index(repo, exclude), path, level), views.cycles, as_json)


@app.command("unused")
def unused(
    path: str = typer.Argument("", help="Only files under this folder."),
    as_json: bool = _JSON,
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """Definitions nothing names, and those only tests name."""
    from . import measures, views

    _emit(measures.unused(_index(repo, exclude), path), views.unused, as_json)


@app.command("diff")
def diff(
    base: str = typer.Argument(
        "", help="A commit; default: the merge-base with origin/main."
    ),
    as_json: bool = _JSON,
    repo: Path | None = _REPO,
    exclude: list[str] = _EXCLUDE,
) -> None:
    """What changed since base, per definition: sizes before and after, and its users."""
    from . import measures, views

    _emit(measures.diff(_index(repo, exclude), base or None), views.diff, as_json)
