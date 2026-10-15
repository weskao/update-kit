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
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from importlib import metadata
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse
from urllib.request import url2pathname

__version__ = "0.1.0"

__all__ = [
    "UpdateError", "check", "hint", "install_kind", "latest_tag", "normalize_lang",
    "should_hint", "update",
]

_IS_WINDOWS = os.name == "nt"  # the running exe is locked there; tests patch this flag

_STRICT = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")
# cmd.exe would interpret these inside the detached chain; such updates are refused.
_CMD_META = re.compile(r'[&|<>^%"()!\r\n]')

_MESSAGES = {
    "en": {
        "dev": "{app}: dev install from {dir}; use git pull to update",
        "check_failed": "{app}: couldn't check for updates ({error})",
        "up_to_date": "{app} {current} is already up to date",
        "manual": "{app} {latest} is available (you have {current}). Install it manually:\n  {command}",
        "installing": "Updating {app} {current} -> {latest} ...",
        "install_failed": "{app}: update failed ({error})",
        "updated": "{app} updated to {latest}",
        "after_failed": "warning: `{step}` failed after the update ({error})",
        "background": "{app} {latest}: update continues in the background (log: {log})",
        "hint": "{cmd} {latest} is available (you have {current}) — run `{cmd} update`",
    },
    "zh-TW": {
        "dev": "{app}：開發版安裝（{dir}），請用 git pull 更新",
        "check_failed": "{app}：無法檢查更新（{error}）",
        "up_to_date": "{app} {current} 已是最新版本",
        "manual": "{app} {latest} 可更新（目前為 {current}），請手動安裝：\n  {command}",
        "installing": "正在更新 {app} {current} -> {latest} ...",
        "install_failed": "{app}：更新失敗（{error}）",
        "updated": "{app} 已更新至 {latest}",
        "after_failed": "警告：更新後執行 `{step}` 失敗（{error}）",
        "background": "{app} {latest}：更新將在背景繼續（記錄檔：{log}）",
        "hint": "{cmd} {latest} 可更新（目前為 {current}）— 執行 `{cmd} update`",
    },
}


class UpdateError(Exception):
    """The latest release could not be determined."""


def _parse(version: str | None) -> tuple[int, int, int] | None:
    match = _STRICT.fullmatch(version.strip()) if version else None
    return tuple(int(part) for part in match.groups()) if match else None


def _repo_url(repo: str, url: str | None) -> str:
    return url or os.environ.get("UPDATE_KIT_REPO_URL") or f"https://github.com/{repo}.git"


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
    target = _repo_url(repo, url)
    # No credential/passphrase prompt for a private/missing repo: fail instead of blocking.
    # Low-speed limits stop a stalled HTTP transfer that outlives the killed git process.
    env = {
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_HTTP_LOW_SPEED_LIMIT": "1",
        "GIT_HTTP_LOW_SPEED_TIME": str(max(1, round(timeout))),
    }
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", target],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=timeout, env=env, check=False,
        )
    except FileNotFoundError as exc:
        raise UpdateError("git not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise UpdateError(str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise UpdateError(detail[-1] if detail else f"git ls-remote exited {result.returncode}")
    tags = [line.rpartition("refs/tags/")[2] for line in result.stdout.splitlines() if "refs/tags/" in line]
    versions = [(parsed, tag) for tag in tags if (parsed := _parse(tag))]
    if not versions:
        raise UpdateError(f"no release tags in {target}")
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


def _windows_chain(commands: Sequence[Sequence[str]], log: str) -> str:
    """One cmd.exe line: wait for the caller to exit (its exe is locked), then run *commands*."""
    chain = " && ".join(subprocess.list2cmdline(list(argv)) for argv in commands)
    wait = "ping -n 4 127.0.0.1 >nul"  # `timeout` refuses to run without a console
    return f'cmd.exe /d /s /c "{wait} & ({chain}) > {subprocess.list2cmdline([log])} 2>&1"'


def _spawn_detached(cmdline: str, log: str) -> None:  # pragma: no cover - Windows only
    os.makedirs(os.path.dirname(log), exist_ok=True)
    # CREATE_NO_WINDOW (not DETACHED_PROCESS): children inherit a hidden console, no flashing.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmdline, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def update(*, app: str, repo: str, dist: str, current: str | None = None, lang: str = "en",
           after: Sequence[Sequence[str]] = (), out: TextIO = sys.stderr,
           run: Callable = subprocess.run, url: str | None = None) -> int:
    """Install the newest release. 0 updated/up to date, 1 failure, 2 dev/unknown install."""
    kind, detail = install_kind(dist)
    if kind == "dev":
        _say(out, _msg(lang, "dev", app=app, dir=detail))
        return 2
    if current is None:
        try:
            current = metadata.version(dist)
        except metadata.PackageNotFoundError:
            pass
    try:
        latest = latest_tag(repo, url=url)
    except UpdateError as exc:
        _say(out, _msg(lang, "check_failed", app=app, error=exc))
        return 1
    have = _parse(current)
    if have and have >= _parse(latest):
        _say(out, _msg(lang, "up_to_date", app=app, current=current))
        return 0
    # An unparseable/unknown current version is treated as older: the user asked to update.
    spec = f"git+{_repo_url(repo, url)}@{latest}"
    shown = current or "?"
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
            _spawn_detached(_windows_chain([install, *after], log), log)
        except OSError as exc:
            _say(out, _msg(lang, "install_failed", app=app, error=exc))
            return 1
        _say(out, _msg(lang, "background", app=app, latest=latest, log=log))
        return 0
    _say(out, _msg(lang, "installing", app=app, current=shown, latest=latest))
    try:
        code = run(install).returncode
    except OSError as exc:
        _say(out, _msg(lang, "install_failed", app=app, error=exc))
        return 1
    if code != 0:
        _say(out, _msg(lang, "install_failed", app=app, error=f"exit {code}"))
        return 1
    _say(out, _msg(lang, "updated", app=app, latest=latest))
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


def check(*, repo: str, current: str, cache_path: Path | None = None, ttl: int = 86400,
          timeout: float = 1.5, url: str | None = None) -> str | None:
    """Newer release tag than *current*, or None. Cached per repo for *ttl*; never raises."""
    try:
        path = Path(cache_path) if cache_path else Path.home() / ".update-kit" / "state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        entry = state.get(repo)
        now = time.time()
        checked = entry.get("checked") if isinstance(entry, dict) else None
        cached = entry.get("latest") if isinstance(entry, dict) else None
        valid = isinstance(checked, (int, float)) and (cached is None or isinstance(cached, str))
        if valid and 0 <= now - checked < ttl:
            latest = cached
        else:
            try:
                latest = latest_tag(repo, timeout=timeout, url=url)
            except Exception:  # noqa: BLE001 - offline, rate-limited, anything: negative-cache it too
                latest = None
            # shortcut: read-merge-replace without a lock; a concurrent writer can drop one
            # entry, which only costs one extra ls-remote later. Add a lock if that matters.
            state[repo] = {"checked": now, "latest": latest}
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
