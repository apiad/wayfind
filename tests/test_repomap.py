"""repomap on a small git repository built per test, through the CLI."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repomap.cli import app
from repomap.index import Index

FILES = {
    "pkg/__init__.py": '"""The package."""\n',
    "pkg/store.py": '''"""Where records live."""


class Store:
    """Keeps records in memory."""

    def put(self, key, value):
        self.data[key] = value

    def get(self, key):
        """One record."""
        return self.data[key]


def helper(x):
    return x + 1
''',
    "pkg/api.py": '''"""The API over the store."""
from pkg.store import Store, helper


def save(key, value):
    s = Store()
    s.put(key, helper(value))


def handle(request):
    save(request.key, request.value)
''',
    "pkg/other.py": """def helper(y):
    return y


def close():
    return helper(2)
""",
    "web/app.ts": """// The web entry point.
import { render } from "./view";

export function main(): void {
  render("hello");
}
""",
    "web/view.ts": """export function render(text: string): void {
  console.log(text);
}
""",
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    # The map's cache only: the grammars stay in the user's cache, downloaded once.
    monkeypatch.setattr("repomap.cli.cache_dir", lambda: tmp_path / "cache")
    return root


def run(repo: Path, *args: str) -> str:
    r = CliRunner().invoke(app, [*args, "--repo", str(repo)])
    assert r.exit_code == 0, r.output
    return r.stdout


def test_repo_lists_folders_and_module_docs(repo):
    out = run(repo, "repo", "pkg")
    assert "store.py  1 class 1 function 2 method  — Where records live." in out
    assert "api.py  2 function  — The API over the store." in out


def test_outline_nests_methods_under_their_class(repo):
    out = run(repo, "outline", "pkg/store.py")
    assert "4: class Store  — Keeps records in memory." in out
    assert "  10: def get(self, key)  — One record." in out


def test_callers_marks_an_import_confirmed_edge_sure(repo):
    out = run(repo, "callers", "pkg/store.py:helper")
    assert "L1 pkg/api.py: save:7\n" in out
    # other.py calls its own helper, which is not this one
    assert "other.py" not in out


def test_callers_of_an_ambiguous_name_are_marked_unsure(repo):
    out = run(repo, "callers", "Store.put")
    assert "pkg/api.py: save:7" in out
    assert "<module>" not in out


def test_impact_follows_sure_edges_to_the_next_level(repo):
    out = run(repo, "impact", "pkg/store.py:helper")
    # the import line is a use too, at module level
    assert "L1 pkg/api.py: <module>:2, save:7" in out
    assert "L2 pkg/api.py: handle:11" in out
    assert "= 3 uses in 1 files" in out


def test_callees_and_show(repo):
    assert "pkg/store.py:Store.put" in run(repo, "callees", "save")
    assert "return self.data[key]" in run(repo, "show", "Store.get")
    assert "pkg/other.py:helper" in run(repo, "find", "helper")


def test_typescript_resolves_across_files(repo):
    out = run(repo, "callers", "render")
    assert "L1 web/app.ts: main:5" in out


def test_an_edited_file_is_reparsed_and_the_rest_is_not(repo, tmp_path):
    cache = tmp_path / "cache"
    assert Index(repo, cache).stats.parsed == len(FILES)
    (repo / "pkg/other.py").write_text("def helper(y):\n    return y\n")
    st = (repo / "pkg/other.py").stat()
    os.utime(repo / "pkg/other.py", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    ix = Index(repo, cache)
    assert ix.stats.parsed == 1
    assert "pkg/other.py:close" not in ix.defs


def test_outside_a_git_repository_it_says_so(tmp_path):
    r = CliRunner().invoke(app, ["repo", "--repo", str(tmp_path)])
    assert r.exit_code == 1
    assert "not inside a git repository" in r.output


# Receivers with a stated type: the shape that #491 found wrong in aegis itself,
# a registry whose subsystems are assigned from outside, a protocol host and a
# test call through untyped fixtures.
TYPED = {
    "app/host.py": """class Host:
    def turn_ended(self, s) -> None: ...


class Session:
    def __init__(self, host: Host):
        self._host = host

    def finish(self):
        self._host.turn_ended(self)
""",
    "app/queues.py": """class Queues:
    def turn_ended(self, s):
        return s
""",
    "app/quota.py": """class Quota:
    def turn_ended(self):
        return 1
""",
    "app/registry.py": """from .host import Host


class Registry(Host):
    def __init__(self):
        self.queues = None
        self.quota = None

    def turn_ended(self, s):
        self.queues.turn_ended(s)
        self.quota.turn_ended()
""",
    "app/main.py": """from .queues import Queues
from .quota import Quota
from .registry import Registry as Sessions


def find(x):
    return x


class App:
    def __init__(self):
        self.sessions = Sessions()
        self.queues = Queues()
        reg = self.sessions
        reg.quota = Quota()
""",
    "app/text.py": """import json


def load(raw):
    data = raw.find("x")
    return json.loads(data)
""",
    "tests/test_flow.py": """def test_a_turn(world):
    world.app.sessions.turn_ended(1)
""",
    "web/svc.ts": """import { Store as Db } from "./store";

export class Svc {
  private db: Db;
  constructor() { this.db = new Db(); }
  run(): void { this.db.put("a"); }
}
""",
    "web/store.ts": """export class Store {
  put(k: string): void {}
}
""",
}


@pytest.fixture
def typed(tmp_path, monkeypatch):
    root = tmp_path / "typed"
    for rel, text in TYPED.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    monkeypatch.setattr("repomap.cli.cache_dir", lambda: tmp_path / "cache")
    return root


def test_callees_follow_the_type_of_each_receiver(typed):
    out = run(typed, "callees", "Registry.turn_ended")
    # self.queues is None in its class; App assigns a Queues to an attribute of
    # that name, and reg.quota is assigned through a Registry-typed receiver
    assert "10: app/queues.py:Queues.turn_ended\n" in out
    assert "11: app/quota.py:Quota.turn_ended\n" in out
    assert "Registry.turn_ended\n" not in out.split("\n", 1)[1]


def test_a_call_through_a_base_reaches_the_override(typed):
    out = run(typed, "callers", "Registry.turn_ended")
    assert "L1 app/host.py: Session.finish:10\n" in out
    # world.app is typed nowhere in the test, but every `.app` names an App
    assert "L1 tests/test_flow.py: test_a_turn:2\n" in out
    assert "?" not in out


def test_a_name_on_an_unknown_receiver_is_no_module_function(typed):
    # raw.find() is a member call: it cannot be main.py's top-level find
    assert "text.py" not in run(typed, "callers", "app/main.py:find")


def test_an_aliased_import_resolves_to_its_class(typed):
    assert "L1 app/main.py: App.__init__:12" in run(typed, "callers", "Registry")


def test_typescript_resolves_a_field_through_new(typed):
    assert "web/store.ts:Store.put\n" in run(typed, "callees", "Svc.run")


def test_exclude_keeps_files_out_of_views_and_in_the_cache(typed, tmp_path):
    run(typed, "repo")
    out = run(typed, "repo", "--exclude", "tests", "-x", "web/*.ts")
    assert "tests/" not in out and "web/" not in out
    ix = Index(typed, tmp_path / "cache")  # every file again: none re-parsed
    assert ix.stats.parsed == 0 and "tests/test_flow.py" in ix.files


def test_a_cold_index_in_a_pool_matches_one_in_process(typed, tmp_path, monkeypatch):
    serial = Index(typed, tmp_path / "a")
    monkeypatch.setattr("repomap.index.POOL_MIN", 1)
    pooled = Index(typed, tmp_path / "b")
    assert pooled.stats.parsed == serial.stats.parsed == len(TYPED)
    assert {k: v[1].refs for k, v in pooled.files.items()} == {
        k: v[1].refs for k, v in serial.files.items()
    }


METRICS = {
    "m.py": '''def branchy(a: int, b, *rest) -> int:
    """Doc."""
    if a:
        pass
    elif b:
        pass
    for x in rest:
        while x:
            break
    try:
        pass
    except ValueError:
        pass
    except KeyError:
        pass
    else:
        pass

    def inner():
        return a and b

    return 1


def plain(self):
    return [x for x in range(3) if x]


class C:
    def m(self, k: "C | None" = None):
        return k
''',
    "a.py": "from b import g\n\n\ndef f():\n    return g()\n",
    "b.py": "from a import f\n\n\ndef g():\n    return f()\n\n\ndef dead():\n    return 0\n\n\ndef probe():\n    return 1\n",
    "tests/test_b.py": "from b import probe\n\n\ndef test_probe():\n    assert probe()\n",
}


@pytest.fixture
def small(tmp_path, monkeypatch):
    root = tmp_path / "small"
    for rel, text in METRICS.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    monkeypatch.setattr("repomap.cli.cache_dir", lambda: tmp_path / "cache")
    return root


def test_metrics_counts_as_ruff_c901_does(small):
    data = json.loads(run(small, "metrics", "--json"))
    fs = {f["name"]: f for f in data["functions"]}
    # if, elif, for, while, two excepts, a try's else and the nested def;
    # not the `and`, not the comprehension
    assert fs["branchy"]["complexity"] == 9
    assert fs["inner"]["complexity"] == 1
    assert fs["plain"]["complexity"] == 1
    assert (fs["branchy"]["lines"], fs["branchy"]["doc"]) == (22, True)
    assert (
        fs["branchy"]["params"],
        fs["branchy"]["typed"],
        fs["branchy"]["returns"],
    ) == (3, 1, True)
    assert (fs["m"]["params"], fs["m"]["typed"]) == (1, 1)
    assert fs["g"]["fan_in"] == 1 and fs["f"]["fan_out"] == 1
    files = {f["file"]: f for f in data["files"]}
    assert files["m.py"]["lines"] == 31 and files["m.py"]["lang"] == "python"


def test_cycles_find_files_that_use_each_other(small):
    out = run(small, "cycles")
    assert "2 files: a.py → b.py → a.py" in out


def test_unused_lists_dead_code_and_test_only_code(small):
    data = json.loads(run(small, "unused", "--json"))
    dead = {r["id"] for r in data["unused"]}
    assert "b.py:dead" in dead and "b.py:g" not in dead
    assert [r["id"] for r in data["tests_only"]] == ["b.py:probe"]


def test_diff_reports_each_definition_with_its_users(small):
    git = ["git", "-C", str(small), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    (small / "b.py").write_text(
        "from a import f\n\n\ndef g():\n    if f():\n        return f()\n\n\n"
        "def probe():\n    return 1\n\n\ndef fresh():\n    return 2\n"
    )
    (small / "a.py").write_text(
        "from b import g\n\n\ndef f():\n    return g() + dead()\n"
    )
    data = json.loads(run(small, "diff", "HEAD", "--json"))
    rows = {r["id"]: r for r in data["definitions"]}
    assert rows["b.py:dead"]["status"] == "removed"
    assert rows["b.py:dead"]["still_named"] == []  # a.py:f is in the diff itself
    assert rows["b.py:g"]["status"] == "modified"
    assert rows["b.py:g"]["complexity"] == [1, 2]
    assert rows["b.py:fresh"]["status"] == "added"
    assert "b.py:probe" not in rows
    assert data["definitions"][0]["id"] == "b.py:dead"  # removals first
