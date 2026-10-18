"""Self-update for git-tag-released Python CLIs (uv tool / pipx). Stdlib only.

    import sys
    import update_kit

    # `<cmd> update` / `<cmd> --update`
    sys.exit(update_kit.update(app="my-cli", repo="owner/my-cli", dist="my-cli"))

    # background hint, TTY stderr only
    if update_kit.should_hint():
        latest = update_kit.check(repo="owner/my-cli", current=__version__)
        if latest:
            print(update_kit.hint("my-cli", latest, __version__), file=sys.stderr)

Releases are strict ``vX.Y.Z`` git tags, discovered with ``git ls-remote``.
Never prompts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from importlib import metadata
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse
from urllib.request import url2pathname

__version__ = "0.2.1"

__all__ = [
    "UpdateError", "check", "hint", "install_kind", "latest_tag", "normalize_lang",
    "should_hint", "update",
]

_IS_WINDOWS = os.name == "nt"  # the running exe is locked there; tests patch this flag
_NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # fail instead of asking for credentials

_STRICT = re.compile(r"v?([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_USERINFO = re.compile(r"(?<=://)[^/\s]*@")
# Only these remote forms reach git; no whitespace/control chars, no userinfo (credentials).
_URL_OK = re.compile(r"(?:https://(?![^/?#]*@)|ssh://(?![^/?#]*:[^/?#]*@)|file://)[^\s\x00-\x1f\x7f-\x9f]+|git@[A-Za-z0-9][A-Za-z0-9._-]*:[^\s\x00-\x1f\x7f-\x9f]+")
# cmd.exe would interpret these inside the detached chain; such updates are refused.
_CMD_META = re.compile(r'[&|<>^%"()!\r\n]')

_MESSAGES = {
    "en": {
        "check_failed": "❌ {app}: couldn't check for updates ({error})",
        "up_to_date": "✅ {app} {current} is already the latest version ({latest}).",
        "manual": "{app} {latest} is available (you have {current}). Install it manually:\n  {command}",
        "installing": "⬆️  Updating {app} {current} → {latest} via `{command}` ...",
        "install_failed": "❌ {app}: update failed ({error})",
        "updated": "🎉 {app} updated to {latest}!",
        "left_alone": "ℹ️  Installed as a {tool} tool; the copy at {dir} is unchanged.",
        "after_failed": "warning: `{step}` failed after the update ({error})",
        "background": "{app} {latest}: update continues in the background (log: {log})",
        "hint": "{cmd} {latest} is available (you have {current}) — run `{cmd} update`",
        "source": "{app}: using update source {url} (UPDATE_KIT_REPO_URL)",
    },
    "zh-TW": {
        "check_failed": "❌ {app}：無法檢查更新（{error}）",
        "up_to_date": "✅ {app} {current} 已是最新版本（{latest}）。",
        "manual": "{app} {latest} 可更新（目前為 {current}），請手動安裝：\n  {command}",
        "installing": "⬆️  正在更新 {app} {current} → {latest}，執行 `{command}` ...",
        "install_failed": "❌ {app}：更新失敗（{error}）",
        "updated": "🎉 {app} 已更新至 {latest}！",
        "left_alone": "ℹ️  已安裝為 {tool} 工具；{dir} 的原本那份沒有變更。",
        "after_failed": "警告：更新後執行 `{step}` 失敗（{error}）",
        "background": "{app} {latest}：更新將在背景繼續（記錄檔：{log}）",
        "hint": "{cmd} {latest} 可更新（目前為 {current}）— 執行 `{cmd} update`",
        "source": "{app}：使用更新來源 {url}（UPDATE_KIT_REPO_URL）",
    },
}


class UpdateError(Exception):
    """The latest release could not be determined."""


def _parse(version: str | None) -> tuple[int, int, int] | None:
    match = _STRICT.fullmatch(version.strip()) if version else None
    return tuple(int(part) for part in match.groups()) if match else None


def _clean(text: str) -> str:
    """Printable, credential-free *text* (terminal escapes and URL userinfo removed)."""
    return _USERINFO.sub("", _CONTROL.sub("", text))


def _repo_url(repo: str, url: str | None) -> str:
    target = url or os.environ.get("UPDATE_KIT_REPO_URL") or f"https://github.com/{repo}.git"
    if not _URL_OK.fullmatch(target):
        raise UpdateError(f"unsupported repo URL: {_clean(target)[:200]!r}")
    return target


def _msg(lang: str, key: str, **fields) -> str:
    return _MESSAGES[normalize_lang(lang)][key].format(**fields)


def _say(out: TextIO, text: str) -> None:
    try:
        print(text, file=out)
    except UnicodeEncodeError:  # e.g. a cp1252 stream on Windows receiving zh-TW
        encoding = getattr(out, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding), file=out)


def latest_tag(repo: str, *, timeout: float = 5, url: str | None = None) -> str:
    """Newest strict ``vX.Y.Z`` tag of github.com/<repo>; raises UpdateError."""
    # Validated before any git call: a value like "--upload-pack=..." must never reach argv.
    target = _repo_url(repo, url)
    # No credential/passphrase prompt for a private/missing repo: fail instead of blocking.
    # Low-speed limits stop a stalled HTTP transfer that outlives the killed git process.
    env = {
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
        **os.environ,
        **_NO_PROMPT,
        "GIT_HTTP_LOW_SPEED_LIMIT": "1",
        "GIT_HTTP_LOW_SPEED_TIME": str(max(1, round(timeout))),
    }
    try:
        result = subprocess.run(
            ["git", "-c", "protocol.ext.allow=never", "ls-remote", "--tags", "--refs", "--", target],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=timeout, env=env, check=False,
            cwd=Path.home(),  # a repo-local config/hook in the caller's cwd must not apply
        )
    except FileNotFoundError as exc:
        raise UpdateError("git not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise UpdateError(str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise UpdateError(_clean(detail[-1]) if detail else f"git ls-remote exited {result.returncode}")
    tags = [line.split("\trefs/tags/", 1)[1] for line in result.stdout.splitlines() if "\trefs/tags/" in line]
    versions = [(tuple(map(int, m.groups())), tag) for tag in tags if (m := _STRICT.fullmatch(tag))]
    if not versions:
        raise UpdateError(f"no release tags in {_clean(target)}")
    return max(versions)[1]


def install_kind(dist: str) -> tuple[str, str | None]:
    """("uv" | "pipx" | "dev" | "unknown", detail) for the installed *dist*."""
    try:
        distribution = metadata.distribution(dist)
    except metadata.PackageNotFoundError:
        return "unknown", None
    try:
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    except (OSError, ValueError):
        direct = {}
    if isinstance(direct, dict) and "dir_info" in direct:  # PEP 610: editable or local dir
        try:
            return "dev", url2pathname(urlparse(direct.get("url", "")).path)
        except Exception:  # noqa: BLE001 - malformed metadata must not break `update`
            return "dev", str(direct.get("url"))
    prefix = Path(sys.prefix)
    parents = [part.lower() for part in prefix.parent.parts]
    # uv: <uv data>/tools/<name> (UV_TOOL_DIR overrides); pipx: <pipx home>/venvs/<name>.
    tool_dir = os.environ.get("UV_TOOL_DIR")
    if (parents[-1:] == ["tools"] and "uv" in parents) or (tool_dir and prefix.parent == Path(tool_dir)):
        return "uv", str(prefix)
    pipx_home = os.environ.get("PIPX_HOME")
    if (parents[-1:] == ["venvs"] and "pipx" in parents) or (
        pipx_home and prefix.parent == Path(pipx_home, "venvs")
    ):
        return "pipx", str(prefix)
    return "unknown", str(prefix)


def _installed_revision(dist: str) -> str | None:
    """The git ref a PEP 610 VCS install was requested at (``v1.2.0``, ``main``); ``""`` for
    a VCS install with no ref (the default branch); None when it is not a VCS install."""
    try:
        direct = json.loads(metadata.distribution(dist).read_text("direct_url.json") or "{}")
        revision = direct["vcs_info"].get("requested_revision")
        return revision if isinstance(revision, str) else ""
    except Exception:  # noqa: BLE001 - not installed, not a VCS install, malformed record
        return None


def _windows_chain(commands: Sequence[Sequence[str]], log: str) -> str:
    """One cmd.exe line: wait for the caller to exit (its exe is locked), then run *commands*."""
    chain = " && ".join(subprocess.list2cmdline(list(argv)) for argv in commands)
    wait = "ping -n 4 127.0.0.1 >nul"  # `timeout` refuses to run without a console
    return f'cmd.exe /d /s /c "{wait} & ({chain}) > {subprocess.list2cmdline([log])} 2>&1"'


def _spawn_detached(cmdline: str, log: str, env: dict | None = None) -> None:  # pragma: no cover - Windows only
    os.makedirs(os.path.dirname(log), exist_ok=True)
    # CREATE_NO_WINDOW (not DETACHED_PROCESS): children inherit a hidden console, no flashing.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    # cwd=home: cmd.exe searches the cwd first, so a planted uv.bat/crw.exe there must not run.
    subprocess.Popen(cmdline, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(Path.home()), env=env)


def update(*, app: str, repo: str, dist: str, current: str | None = None, lang: str = "en",
           after: Sequence[Sequence[str]] = (), out: TextIO = sys.stderr,
           run: Callable = subprocess.run, url: str | None = None) -> int:
    """Install the newest release tag. 0 updated/up to date, 1 failure, 2 no installer found.

    Up to date means installed from exactly that tag; anything else (a dev checkout, a
    branch or commit, a newer build, an unmanaged install) is replaced by it."""
    kind, detail = install_kind(dist)
    if current is None:
        try:
            current = metadata.version(dist)
        except metadata.PackageNotFoundError:
            pass
    try:
        target = _repo_url(repo, url)
        if not url and os.environ.get("UPDATE_KIT_REPO_URL"):
            _say(out, _msg(lang, "source", app=app, url=_clean(target)))
        latest = latest_tag(repo, url=target)
    except UpdateError as exc:
        _say(out, _msg(lang, "check_failed", app=app, error=exc))
        return 1
    revision = _installed_revision(dist)
    if kind in ("uv", "pipx") and _parse(current) == _parse(latest) and revision in (None, latest):
        _say(out, _msg(lang, "up_to_date", app=app, current=current, latest=latest))
        return 0
    # An unparseable/unknown current version is treated as older: the user asked to update.
    # pip/uv can't parse the scp form (git@host:path); install over the equivalent ssh:// URL.
    source = re.sub(r"^git@([^:]+):/?", r"ssh://git@\1/", target)
    spec = f"git+{source}@{latest}"
    shown = current or "?"
    # A checkout or unmanaged install becomes a separate tool install of the tag; it stays as it was.
    left_alone = detail if kind in ("dev", "unknown") else None
    if kind in ("dev", "unknown"):
        kind = next((tool for tool in ("uv", "pipx") if shutil.which(tool)), "unknown")
    note = _msg(lang, "left_alone", tool=kind, dir=left_alone) if left_alone else None
    if kind == "uv":
        install = ["uv", "tool", "install", "--force", "--no-sources", spec]
    elif kind == "pipx":
        install = ["pipx", "install", "--force", spec]
    else:
        argv = ["python", "-m", "pip", "install", "--upgrade", spec]
        command = subprocess.list2cmdline(argv) if _IS_WINDOWS else shlex.join(argv)
        _say(out, _msg(lang, "manual", app=app, latest=latest, current=shown, command=command))
        return 2
    if _IS_WINDOWS:
        log = os.path.join(os.path.expanduser("~"), ".update-kit", f"{app}-update.log")
        unsafe = [arg for arg in [log, *install, *(a for step in after for a in step)] if _CMD_META.search(arg)]
        if unsafe:
            _say(out, _msg(lang, "install_failed", app=app, error=f"unsafe for cmd.exe: {unsafe[0]}"))
            return 1
        try:
            _spawn_detached(_windows_chain([install, *after], log), log, {**os.environ, **_NO_PROMPT})
        except OSError as exc:
            _say(out, _msg(lang, "install_failed", app=app, error=exc))
            return 1
        _say(out, _msg(lang, "background", app=app, latest=latest, log=log))
        if note:
            _say(out, note)
        return 0
    _say(out, _msg(lang, "installing", app=app, current=shown, latest=latest, command=shlex.join(install)))
    try:
        code = run(install, cwd=Path.home(), env={**os.environ, **_NO_PROMPT}).returncode
    except OSError as exc:
        _say(out, _msg(lang, "install_failed", app=app, error=exc))
        return 1
    if code != 0:
        _say(out, _msg(lang, "install_failed", app=app, error=f"exit {code}"))
        return 1
    _say(out, _msg(lang, "updated", app=app, latest=latest))
    if note:
        _say(out, note)
    result = 0
    for step in after:
        step = list(step)
        try:
            step_code, error = run(step).returncode, None
        except OSError as exc:
            step_code, error = -1, exc
        if step_code != 0:
            _say(out, _msg(lang, "after_failed", step=" ".join(step), error=error or f"exit {step_code}"))
            result = 1
    return result


def check(*, repo: str, current: str, cache_path: Path | None = None, ttl: int = 3600,
          timeout: float = 1.5, url: str | None = None) -> str | None:
    """Newer release tag than *current*, or None. Cached per source URL for *ttl*; never raises."""
    try:
        path = Path(cache_path) if cache_path else Path.home() / ".update-kit" / "state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        key = _repo_url(repo, url)  # keyed by source, so an override can't poison the normal entry
        entry = state.get(key)
        now = time.time()
        checked = entry.get("checked") if isinstance(entry, dict) else None
        cached = entry.get("latest") if isinstance(entry, dict) else None
        valid = isinstance(checked, (int, float)) and (
            cached is None or (isinstance(cached, str) and _STRICT.fullmatch(cached) is not None)
        )
        if valid and 0 <= now - checked < ttl:
            latest = cached
        else:
            try:
                latest = latest_tag(repo, timeout=timeout, url=url)
            except Exception:  # noqa: BLE001 - offline, rate-limited, anything: negative-cache it too
                latest = None
            # shortcut: read-merge-replace without a lock; a concurrent writer can drop one
            # entry, which only costs one extra ls-remote later. Add a lock if that matters.
            state[key] = {"checked": now, "latest": latest}
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        have, new = _parse(current), _parse(latest)
        return latest if have and new and new > have else None
    except Exception:  # noqa: BLE001 - check() never raises
        return None


def hint(cmd: str, latest: str, current: str, lang: str = "en") -> str:
    """One-line "update available" notice naming the command the user actually ran."""
    return _msg(lang, "hint", cmd=cmd, latest=latest, current=current)


def should_hint(stream: TextIO = sys.stderr) -> bool:
    """True only when *stream* is a TTY (never under launchd/timers/pipes/--json)."""
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - closed/None/odd streams mean "no"
        return False


def normalize_lang(value: str | None) -> str:
    """zh_TW / zh-tw / zh-Hant / zh_HK -> "zh-TW"; anything else -> "en"."""
    tag = re.split(r"[.@]", value or "")[0].replace("_", "-").lower()
    return "zh-TW" if tag in ("zh-tw", "zh-hk", "zh-mo") or tag.startswith("zh-hant") else "en"
