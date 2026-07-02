from pathlib import Path

from pico.repo_map import RepoMap


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_scan_python_class_and_function(tmp_path):
    _write(tmp_path, "pico/auth.py", "class AuthMiddleware:\n    pass\n\ndef hash_password():\n    pass\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    hits = rm.lookup("AuthMiddleware")
    assert len(hits) == 1
    assert hits[0].file == "pico/auth.py"
    assert hits[0].kind == "class"
    assert hits[0].line == 1

    hits = rm.lookup("hash_password")
    assert hits[0].kind == "function"


def test_scan_python_method(tmp_path):
    _write(tmp_path, "pico/x.py", "class Foo:\n    def bar(self):\n        pass\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    hits = rm.lookup("bar")
    assert hits and hits[0].kind == "method"


def test_scan_typescript_class_and_function(tmp_path):
    _write(tmp_path, "src/auth.ts", "export class AuthGuard {}\nexport function login() {}\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    hits = rm.lookup("AuthGuard")
    assert hits and hits[0].file == "src/auth.ts"
    hits = rm.lookup("login")
    assert hits and hits[0].kind == "function"


def test_scan_go_func_and_struct(tmp_path):
    _write(tmp_path, "main.go", "func Login() {}\ntype AuthGuard struct {}\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    assert rm.lookup("Login")
    assert rm.lookup("AuthGuard")


def test_scan_rust_fn_and_struct(tmp_path):
    _write(tmp_path, "src/main.rs", "pub fn login() {}\npub struct AuthGuard;\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    assert rm.lookup("login")
    assert rm.lookup("AuthGuard")


def test_skip_ignored_dirs(tmp_path):
    _write(tmp_path, ".venv/lib/foo.py", "class Ignored: pass\n")
    _write(tmp_path, "node_modules/pkg/x.js", "class Ignored {}\n")
    _write(tmp_path, "pico/kept.py", "class Kept: pass\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    assert rm.lookup("Ignored") == []
    assert rm.lookup("Kept")


def test_top_level_tree(tmp_path):
    _write(tmp_path, "pico/a.py", "")
    _write(tmp_path, "pico/b.py", "")
    _write(tmp_path, "tests/t.py", "")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    tree = {e["path"]: e for e in rm.top_level_tree()}
    assert tree["pico"]["file_count"] == 2
    assert tree["tests"]["file_count"] == 1


def test_lookup_with_kind_filter(tmp_path):
    _write(tmp_path, "a.py", "class Foo: pass\ndef Foo_helper(): pass\n")
    _write(tmp_path, "b.py", "def Foo(): pass\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    assert all(h.kind == "class" for h in rm.lookup("Foo", kind="class"))
    assert all(h.kind == "function" for h in rm.lookup("Foo", kind="function"))


def test_refresh_incremental(tmp_path):
    p = tmp_path / "pico" / "auth.py"
    p.parent.mkdir(parents=True)
    p.write_text("class Old: pass\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    assert rm.lookup("Old")

    # Modify content, then refresh
    import time
    time.sleep(0.01)  # ensure mtime bump
    p.write_text("class New: pass\n")
    rm.refresh_if_stale()
    assert rm.lookup("New")
    assert not rm.lookup("Old")


def test_syntax_error_does_not_crash(tmp_path):
    _write(tmp_path, "bad.py", "def broken(:\n")
    _write(tmp_path, "good.py", "def ok(): pass\n")
    rm = RepoMap(repo_root=tmp_path)
    rm.scan()
    assert rm.lookup("ok")
