import io
import json
import subprocess
import time
from pathlib import Path

import pytest

import update_kit as uk

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- helpers


class FakeRun:
    """Records argv lists; returns a CompletedProcess with the scripted return code."""

    def __init__(self, codes=None, raise_on=None):
        self.calls = []
        self.codes = codes or {}
        self.raise_on = raise_on

    def __call__(self, argv, *args, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if self.raise_on and argv[0] == self.raise_on:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, self.codes.get(argv[0], 0))


@pytest.fixture
def isolated_git(monkeypatch, tmp_path):
    # The user's global git config (signing, insteadOf rewrites) must not leak into tests.
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("UPDATE_KIT_REPO_URL", raising=False)


def make_repo(path, tags):
    path.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=user@example.com",
        "commit", "--allow-empty", "-q", "-m", "init")
    for tag in tags:
        git("tag", tag)
    return path.as_uri()


@pytest.fixture
def fake_env(monkeypatch):
    """update() with a uv install, latest v1.1.0, no network."""
    state = {"kind": ("uv", None), "latest": "v1.1.0", "tag_calls": []}
    # Real Windows CI must not spawn a detached installer from the POSIX-path tests.
    monkeypatch.setattr(uk, "_IS_WINDOWS", False)

    def latest_tag(repo, *, timeout=5, url=None):
        state["tag_calls"].append((repo, url))
        if isinstance(state["latest"], Exception):
            raise state["latest"]
        return state["latest"]

    monkeypatch.setattr(uk, "install_kind", lambda dist: state["kind"])
    monkeypatch.setattr(uk, "latest_tag", latest_tag)
    return state


def do_update(run, **kw):
    out = io.StringIO()
    kw.setdefault("current", "1.0.0")
    code = uk.update(app="demo", repo="owner/demo", dist="demo-dist", out=out, run=run, **kw)
    return code, out.getvalue()


# --------------------------------------------------------------------------- latest_tag


def test_latest_tag_picks_newest_strict_tag_and_ignores_prereleases(tmp_path, isolated_git):
    url = make_repo(tmp_path / "origin", [
        "v0.9.0", "v0.10.0", "0.10.1", "v1.0.0-rc1", "v2.0.0b1", "v3.0", "nightly", "v9.9.9.9",
    ])
    # numeric, not lexical: 0.10.1 > 0.9.0; prereleases and non-matching names ignored
    assert uk.latest_tag("owner/demo", url=url) == "0.10.1"


def test_latest_tag_uses_env_url(tmp_path, isolated_git, monkeypatch):
    monkeypatch.setenv("UPDATE_KIT_REPO_URL", make_repo(tmp_path / "origin", ["v1.2.3"]))
    assert uk.latest_tag("owner/demo") == "v1.2.3"


def test_latest_tag_no_strict_tags_raises(tmp_path, isolated_git):
    url = make_repo(tmp_path / "origin", ["v1.0.0-rc1", "latest"])
    with pytest.raises(uk.UpdateError):
        uk.latest_tag("owner/demo", url=url)


def test_latest_tag_unreachable_repo_raises(tmp_path, isolated_git):
    with pytest.raises(uk.UpdateError):
        uk.latest_tag("owner/demo", url=(tmp_path / "missing").as_uri())


def test_latest_tag_git_missing_raises(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(uk.subprocess, "run", boom)
    with pytest.raises(uk.UpdateError):
        uk.latest_tag("owner/demo", url="https://example.invalid/x.git")


def test_latest_tag_timeout_raises(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(uk.subprocess, "run", slow)
    with pytest.raises(uk.UpdateError):
        uk.latest_tag("owner/demo", url="https://example.invalid/x.git", timeout=1)


def test_latest_tag_default_url_is_github(monkeypatch):
    seen = {}

    def fake(argv, **k):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "abc\trefs/tags/v1.0.0\n", "")

    monkeypatch.delenv("UPDATE_KIT_REPO_URL", raising=False)
    monkeypatch.setattr(uk.subprocess, "run", fake)
    assert uk.latest_tag("owner/demo") == "v1.0.0"
    assert seen["argv"] == ["git", "ls-remote", "--tags", "--refs", "https://github.com/owner/demo.git"]


# --------------------------------------------------------------------------- install_kind


class FakeDist:
    def __init__(self, direct_url):
        self.direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return None if self.direct_url is None else json.dumps(self.direct_url)


def test_install_kind_dev_from_editable_dir(tmp_path, monkeypatch):
    src = tmp_path / "checkout"
    src.mkdir()
    dist = FakeDist({"url": src.as_uri(), "dir_info": {"editable": True}})
    monkeypatch.setattr(uk.metadata, "distribution", lambda name: dist)
    assert uk.install_kind("demo") == ("dev", str(src))


def test_install_kind_dev_for_this_editable_checkout():
    # `uv sync` installs this project editable (PEP 610 dir_info) — a real direct_url.json.
    kind, detail = uk.install_kind("update-kit")
    assert kind == "dev"
    assert Path(detail).resolve() == ROOT


def test_install_kind_uv_from_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(uk.metadata, "distribution", lambda name: FakeDist(None))
    monkeypatch.setattr(uk.sys, "prefix", str(tmp_path / ".local" / "share" / "uv" / "tools" / "demo"))
    assert uk.install_kind("demo") == ("uv", str(tmp_path / ".local" / "share" / "uv" / "tools" / "demo"))


def test_install_kind_uv_git_install_is_not_dev(tmp_path, monkeypatch):
    dist = FakeDist({"url": "https://github.com/owner/demo.git", "vcs_info": {"vcs": "git"}})
    monkeypatch.setattr(uk.metadata, "distribution", lambda name: dist)
    monkeypatch.setattr(uk.sys, "prefix", str(tmp_path / "uv" / "data" / "tools" / "demo"))
    assert uk.install_kind("demo")[0] == "uv"


def test_install_kind_pipx_from_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(uk.metadata, "distribution", lambda name: FakeDist(None))
    monkeypatch.setattr(uk.sys, "prefix", str(tmp_path / ".local" / "pipx" / "venvs" / "demo"))
    assert uk.install_kind("demo")[0] == "pipx"


def test_install_kind_unknown_for_plain_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(uk.metadata, "distribution", lambda name: FakeDist(None))
    monkeypatch.setattr(uk.sys, "prefix", str(tmp_path / "project" / ".venv"))
    assert uk.install_kind("demo")[0] == "unknown"


def test_install_kind_unknown_when_not_installed():
    assert uk.install_kind("definitely-not-installed-dist-xyz") == ("unknown", None)


# --------------------------------------------------------------------------- update


def test_update_uv_installs_tag(fake_env):
    run = FakeRun()
    code, out = do_update(run)
    assert code == 0
    assert run.calls == [["uv", "tool", "install", "--force", "--no-sources",
                          "git+https://github.com/owner/demo.git@v1.1.0"]]
    assert "v1.1.0" in out


def test_update_pipx_installs_tag(fake_env):
    fake_env["kind"] = ("pipx", None)
    run = FakeRun()
    assert do_update(run)[0] == 0
    assert run.calls == [["pipx", "install", "--force", "git+https://github.com/owner/demo.git@v1.1.0"]]


def test_update_passes_url_through(fake_env):
    run = FakeRun()
    do_update(run, url="file:///srv/demo")
    assert fake_env["tag_calls"] == [("owner/demo", "file:///srv/demo")]
    assert run.calls[0][-1] == "git+file:///srv/demo@v1.1.0"


def test_update_already_up_to_date(fake_env):
    run = FakeRun()
    for current in ("1.1.0", "v1.1.0", "1.2.0"):
        code, out = do_update(run, current=current)
        assert code == 0
        assert "already up to date" in out
    assert run.calls == []


def test_update_couldnt_check_is_failure_not_up_to_date(fake_env):
    fake_env["latest"] = uk.UpdateError("offline")
    run = FakeRun()
    code, out = do_update(run)
    assert code == 1
    assert "couldn't check" in out
    assert "up to date" not in out
    assert run.calls == []


def test_update_install_failure_returns_1_and_skips_after(fake_env):
    run = FakeRun(codes={"uv": 2})
    code, out = do_update(run, after=[["demo", "migrate"]])
    assert code == 1
    assert run.calls == [run.calls[0]]
    assert "failed" in out


def test_update_installer_missing_returns_1(fake_env):
    code, out = do_update(FakeRun(raise_on="uv"))
    assert code == 1
    assert "failed" in out


def test_update_runs_after_steps_in_order(fake_env):
    run = FakeRun()
    code, _ = do_update(run, after=[("demo", "migrate"), ["demo", "reload"]])
    assert code == 0
    assert run.calls[1:] == [["demo", "migrate"], ["demo", "reload"]]


def test_update_failing_after_step_warns_and_returns_1(fake_env):
    run = FakeRun(codes={"demo": 3})
    code, out = do_update(run, after=[["demo", "migrate"], ["other", "step"]])
    assert code == 1
    assert "demo migrate" in out
    assert run.calls[-1] == ["other", "step"]


def test_update_dev_install_returns_2_without_network(fake_env):
    fake_env["kind"] = ("dev", "/src/demo")
    run = FakeRun()
    code, out = do_update(run)
    assert code == 2
    assert "/src/demo" in out and "git pull" in out
    assert fake_env["tag_calls"] == [] and run.calls == []


def test_update_unknown_kind_prints_manual_command(fake_env):
    fake_env["kind"] = ("unknown", None)
    run = FakeRun()
    code, out = do_update(run)
    assert code == 2
    assert "git+https://github.com/owner/demo.git@v1.1.0" in out
    assert run.calls == []


def test_update_current_defaults_to_installed_version(fake_env, monkeypatch):
    monkeypatch.setattr(uk.metadata, "version", lambda dist: "1.1.0")
    code, out = do_update(FakeRun(), current=None)
    assert code == 0 and "already up to date" in out


def test_update_zh_tw_messages(fake_env):
    fake_env["latest"] = "v1.0.0"
    code, out = do_update(FakeRun(), lang="zh_TW")
    assert code == 0
    assert "已是最新版本" in out


def test_update_windows_spawns_detached_chain(fake_env, monkeypatch):
    spawned = []
    monkeypatch.setattr(uk, "_spawn_detached", lambda cmdline, log: spawned.append((cmdline, log)))
    monkeypatch.setattr(uk, "_IS_WINDOWS", True)
    run = FakeRun()
    code, out = do_update(run, after=[["demo", "migrate"]])
    assert code == 0
    assert "update continues in the background" in out
    assert run.calls == []
    (cmdline, log), = spawned
    assert log.endswith("demo-update.log")
    install = "uv tool install --force --no-sources git+https://github.com/owner/demo.git@v1.1.0"
    assert install in cmdline
    assert cmdline.index(install) < cmdline.index("&& demo migrate")


def test_windows_chain_quotes_arguments():
    chain = uk._windows_chain([["uv", "tool", "install"], ["my app", "x"]], r"C:\log dir\u.log")
    assert chain.startswith('cmd.exe /d /s /c "')
    assert '&& "my app" x' in chain
    assert r'"C:\log dir\u.log"' in chain


# --------------------------------------------------------------------------- check


@pytest.fixture
def tags(monkeypatch):
    state = {"result": "v1.1.0", "calls": 0}

    def latest_tag(repo, *, timeout=5, url=None):
        state["calls"] += 1
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(uk, "latest_tag", latest_tag)
    return state


def test_check_returns_newer_and_caches(tmp_path, tags):
    cache = tmp_path / "state.json"
    assert uk.check(repo="owner/demo", current="1.0.0", cache_path=cache) == "v1.1.0"
    assert uk.check(repo="owner/demo", current="1.0.0", cache_path=cache) == "v1.1.0"
    assert tags["calls"] == 1
    entry = json.loads(cache.read_text(encoding="utf-8"))["owner/demo"]
    assert entry["latest"] == "v1.1.0"


def test_check_not_newer_returns_none(tmp_path, tags):
    assert uk.check(repo="owner/demo", current="1.1.0", cache_path=tmp_path / "s.json") is None


def test_check_refetches_after_ttl(tmp_path, tags):
    cache = tmp_path / "state.json"
    cache.write_text(json.dumps({"owner/demo": {"checked": time.time() - 100, "latest": "v1.0.5"}}),
                     encoding="utf-8")
    assert uk.check(repo="owner/demo", current="1.0.0", cache_path=cache, ttl=50) == "v1.1.0"
    assert tags["calls"] == 1


def test_check_negative_cache(tmp_path, tags):
    tags["result"] = uk.UpdateError("offline")
    cache = tmp_path / "state.json"
    assert uk.check(repo="owner/demo", current="1.0.0", cache_path=cache) is None
    tags["result"] = "v9.0.0"
    assert uk.check(repo="owner/demo", current="1.0.0", cache_path=cache) is None
    assert tags["calls"] == 1


def test_check_never_raises(tmp_path, tags):
    tags["result"] = RuntimeError("unexpected")
    assert uk.check(repo="owner/demo", current="1.0.0", cache_path=tmp_path / "s.json") is None
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    tags["result"] = "v1.1.0"
    assert uk.check(repo="owner/demo", current="1.0.0", cache_path=corrupt) == "v1.1.0"
    # Unwritable cache location: still answers, still doesn't raise.
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert uk.check(repo="owner/demo", current="1.0.0", cache_path=blocker / "state.json") == "v1.1.0"
    assert uk.check(repo="owner/demo", current="garbage", cache_path=tmp_path / "g.json") is None


def test_check_keeps_other_repos(tmp_path, tags):
    cache = tmp_path / "state.json"
    cache.write_text(json.dumps({"other/repo": {"checked": 1, "latest": "v0.1.0"}}), encoding="utf-8")
    uk.check(repo="owner/demo", current="1.0.0", cache_path=cache)
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"other/repo", "owner/demo"}


def test_check_default_cache_under_home(tmp_path, tags, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    uk.check(repo="owner/demo", current="1.0.0")
    assert (tmp_path / ".update-kit" / "state.json").is_file()


# --------------------------------------------------------------------------- hint / lang / tty


def test_hint_en():
    assert uk.hint("codex-accounts", "v1.2.0", "1.1.0") == (
        "codex-accounts v1.2.0 is available (you have 1.1.0) — run `codex-accounts update`"
    )


def test_hint_zh_tw():
    text = uk.hint("codex-accounts", "v1.2.0", "1.1.0", lang="zh-Hant")
    assert "codex-accounts update" in text and "v1.2.0" in text and "1.1.0" in text
    assert "可更新" in text


@pytest.mark.parametrize("value, expected", [
    ("zh_TW", "zh-TW"), ("zh-tw", "zh-TW"), ("zh-Hant", "zh-TW"), ("zh_HK", "zh-TW"),
    ("zh-Hant-TW", "zh-TW"), ("zh_TW.UTF-8", "zh-TW"), ("zh-TW", "zh-TW"),
    ("en", "en"), ("zh-CN", "en"), ("fr", "en"), ("", "en"), (None, "en"),
])
def test_normalize_lang(value, expected):
    assert uk.normalize_lang(value) == expected


class Tty(io.StringIO):
    def isatty(self):
        return True


class Broken:
    def isatty(self):
        raise ValueError("closed")


def test_should_hint():
    assert uk.should_hint(Tty()) is True
    assert uk.should_hint(io.StringIO()) is False
    assert uk.should_hint(Broken()) is False
    assert uk.should_hint(None) is False


def test_public_api():
    assert uk.__version__ == "0.1.0"
    for name in ("UpdateError", "latest_tag", "install_kind", "update", "check", "hint",
                 "should_hint", "normalize_lang"):
        assert callable(getattr(uk, name))
