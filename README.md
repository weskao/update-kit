# update-kit

Self-update for Python CLIs that are released as git tags (`vX.Y.Z` or `X.Y.Z`) and installed with
`uv tool` or `pipx`. Stdlib only, Python ≥ 3.10, macOS / Linux / Windows.

- `<cmd> update` installs the newest release tag — no prompts, ever.
- A cached background check prints a one-line hint on an interactive terminal only.
- Every message exists in English and Traditional Chinese (`zh-TW`).

## Install

```toml
# consumer pyproject.toml
dependencies = ["update-kit>=0.1,<0.2"]
```

## Wiring a consumer CLI

```python
import sys

import update_kit

from my_cli import __version__

APP, REPO, DIST = "my-cli", "owner/my-cli", "my-cli"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    lang = "zh-TW"  # the app's own current language setting

    # `my-cli update` and `my-cli --update`
    if argv[:1] == ["update"] or "--update" in argv:
        sys.exit(update_kit.update(
            app=APP, repo=REPO, dist=DIST, lang=lang,
            after=[["my-cli", "migrate"]],  # optional: runs with the NEW binary
        ))

    # Background hint: stderr, TTY only (never under launchd/timers/--json/pipes).
    if update_kit.should_hint():
        latest = update_kit.check(repo=REPO, current=__version__)
        if latest:
            print(update_kit.hint("my-cli", latest, __version__, lang), file=sys.stderr)
    ...
```

## API

| Function | Does |
|---|---|
| `latest_tag(repo, *, timeout=5, url=None) -> str` | Newest strict `v?X.Y.Z` tag (ASCII digits) via `git ls-remote --tags --refs`. Prereleases and other names are ignored. Raises `UpdateError` (offline, no tags, git missing). |
| `install_kind(dist) -> (kind, detail)` | `"dev"` (PEP 610 local/editable dir, detail = the dir), `"uv"` / `"pipx"` (from `sys.prefix`), else `"unknown"`. |
| `update(*, app, repo, dist, current=None, lang="en", after=(), out=sys.stderr, run=subprocess.run, url=None) -> int` | Exit code: **0** updated or already up to date · **1** couldn't check (offline/rate limit) or install/after-step failed · **2** dev install (use `git pull`) or unknown install (manual command printed). |
| `check(*, repo, current, cache_path=None, ttl=86400, timeout=1.5, url=None) -> str \| None` | Newer tag or `None`. Never raises. Results — failures too — are cached for `ttl` in `$HOME/.update-kit/state.json`, keyed by the resolved source URL. |
| `hint(cmd, latest, current, lang="en") -> str` | `my-cli v1.2.0 is available (you have 1.1.0) — run` `` `my-cli update` `` |
| `should_hint(stream=sys.stderr) -> bool` | `True` only when `stream` is a TTY. |
| `normalize_lang(value) -> str` | `zh_TW` / `zh-tw` / `zh-Hant` / `zh_HK` → `"zh-TW"`; anything else → `"en"`. |

Install commands used by `update`:

- uv: `uv tool install --force --no-sources git+<url>@<tag>`
- pipx: `pipx install --force git+<url>@<tag>`

`<url>` is `url=`, else `$UPDATE_KIT_REPO_URL`, else `https://github.com/<repo>.git`
(`file://` works, which is how the tests run without a network). Only `https://`, `ssh://`,
`file://` and `git@host:path` are accepted (`ssh://git@host/...` is fine, `ssh://user:pass@` is not; `git@host:path` is installed as `ssh://git@host/path`); a leading `-`, whitespace/control characters,
`ext::` or credentials in the URL (`user:pass@`) raise `UpdateError` (`update` returns 1,
`check` returns `None`). When `$UPDATE_KIT_REPO_URL` is in effect, `update` prints one line
naming it. `git ls-remote` runs as `git -c protocol.ext.allow=never ls-remote --tags --refs -- <url>`
from the home directory, and so does the install; git's stderr is stripped of control characters.

**Windows:** the running `.exe` is locked, so `update` starts the install (and the `after`
steps, chained with `&&`) in a detached `cmd.exe` that waits a few seconds for the app to
exit, returns 0 and says the update continues in the background. Output goes to
`%USERPROFILE%\.update-kit\<app>-update.log`. Unlike POSIX (where every after-step runs and
any failure makes the result 1), the Windows chain stops at the first failing step and the
outcome is only in the log. An install whose arguments or log path contain a `cmd.exe`
metacharacter (`& | < > ^ % " ( ) !`) is refused with exit 1 instead of being spawned.

`git ls-remote` never prompts: `GIT_TERMINAL_PROMPT=0`, and `GIT_SSH_COMMAND` defaults to
`ssh -o BatchMode=yes` when unset. The install step (uv / pipx) also runs with
`GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv build
```

## CI notifications

`.github/workflows/ci.yml` runs the test matrix (macOS/Linux/Windows) on every push and PR,
then a separate `notify-telegram` job sends a Telegram message only when the test job fails on
a `push` (never on green runs, never on `pull_request`, to avoid pinging on forks/external PRs).
Configure it once per repo:

```bash
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

Unconfigured secrets are a valid state — the job skips quietly instead of failing a second time
on top of the real failure.

## License

MIT
