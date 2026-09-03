"""Driving Obsidian's headless Sync client.

``obsidian-headless`` is Obsidian's own CLI. It is a client rather than a
server: it signs in to Obsidian Sync as though it were another of the user's
devices and writes the vault out as plain markdown, which is all the Obsidian
connector needs. Task Hub hosts nothing.

Every command takes flags, so the whole of setup can be driven from the web
interface -- which matters, because the rule in this project is that nothing
requires a terminal.

**On credentials and the command line.** Secrets are passed to ``ob`` as flags,
which puts them in the container's process list for the second or two the
command runs. That is not the preference: the tunnel module goes out of its way
to pass the Cloudflare token through the environment instead, and the same was
attempted here. It does not work. ``ob`` will prompt for anything omitted, but
its prompts are drawn with a terminal UI library that ignores input fed through
a pty -- tried with both line endings, and with waiting for the prompt to settle
-- so the command simply hangs at "Email:" forever. Flags are the interface the
tool actually supports.

What makes that acceptable rather than merely unavoidable: the only things
running in this container are Task Hub, Radicale and cloudflared, all as the
same user. Anything able to read ``/proc`` here can already read ``secret.key``
and the database it decrypts, so the password's brief appearance in a process
list adds no exposure that did not already exist. It would matter on a shared
host, and it is called out in the docs for that reason. The command is never
logged, and output is redacted before it reaches a log or a page.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

from app.config import OBSIDIAN_CONFIG_DIR, OBSIDIAN_VAULTS_DIR

logger = logging.getLogger(__name__)

OB = "/usr/local/bin/ob"

#: Long enough for a first sync of a large vault to make progress, short enough
#: that a wedged child does not hold a request open forever.
DEFAULT_TIMEOUT = 120.0
LOGIN_TIMEOUT = 90.0

#: Anything that looks like a secret, removed before a line is logged or shown.
#: The CLI echoes its own arguments and prompts back, so without this a password
#: could reach the log by way of the transcript.
#:
#: Matched narrowly, on purpose. An earlier version matched the word "password"
#: followed by *any* whitespace and token, which meant the client's own
#: "Password not provided." came out as "Password <redacted> provided." -- an
#: error message reading as the exact opposite of the truth, sending someone off
#: to check a password they had not given. A redaction that rewrites meaning is
#: worse than one that occasionally leaves a value in, so this only fires where
#: a value genuinely follows: after a flag, or after a colon or equals sign.
_SECRET_RE = re.compile(
    r"(--(?:password|token|mfa|secret)[\s=])(\S+)"
    r"|((?:password|token|mfa|secret)\s*[:=]\s*)(\S+)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    def _hide(match: re.Match) -> str:
        prefix = match.group(1) or match.group(3)
        return f"{prefix}<redacted>"

    return _SECRET_RE.sub(_hide, text)


def available() -> bool:
    """Whether the CLI is present in this image at all."""
    return os.path.exists(OB)


@dataclass
class Result:
    ok: bool
    output: str
    code: int = 0
    #: Parsed JSON, for the commands that offer --json.
    data: object | None = None

    @property
    def message(self) -> str:
        """The most useful single line to put in front of a user.

        The CLI reports failures as a Node error dump with a stack trace in the
        middle of it. The first line carries the actual explanation -- "please
        double check your email and password" -- and the rest is noise nobody
        can act on.
        """
        for line in self.output.splitlines():
            line = line.strip()
            if not line or line.startswith("at "):
                continue
            # Node dumps errors as "Login failed: s [Error]: <the real reason>".
            # Only the last part is any use to somebody trying to sign in.
            if "]: " in line:
                line = line.rsplit("]: ", 1)[1]
            return redact(line.removeprefix("Error: ").strip())
        return "The Obsidian client failed without saying why."


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    # The client keeps its login and sync database under XDG_CONFIG_HOME. It is
    # pointed at the data volume so a rebuild does not sign the user out and
    # re-download the whole vault.
    env["XDG_CONFIG_HOME"] = str(OBSIDIAN_CONFIG_DIR)
    env.setdefault("HOME", str(OBSIDIAN_CONFIG_DIR))
    return env


def run(args: list[str], timeout: float = DEFAULT_TIMEOUT) -> Result:
    """Run a command that needs no secrets, and collect its output."""
    if not available():
        return Result(False, "The Obsidian client is not installed in this image.")

    OBSIDIAN_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        finished = subprocess.run(
            [OB, *args], capture_output=True, text=True,
            timeout=timeout, env=_environment(),
        )
    except subprocess.TimeoutExpired:
        return Result(False, f"'ob {args[0]}' took longer than {timeout:.0f}s.")
    except OSError as exc:
        return Result(False, str(exc))

    output = (finished.stdout or "") + (finished.stderr or "")
    result = Result(finished.returncode == 0, output.strip(), finished.returncode)

    if "--json" in args and result.ok:
        result.data = _first_json(result.output)
    return result


def run_secret(args: list[str], timeout: float = LOGIN_TIMEOUT) -> Result:
    """Run a command whose arguments include a secret.

    Identical to :func:`run` except that nothing about the invocation is
    logged -- not the arguments, not on failure, not at debug level. See the
    module docstring for why the secret is on the command line at all.
    """
    if not available():
        return Result(False, "The Obsidian client is not installed in this image.")

    OBSIDIAN_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        finished = subprocess.run(
            [OB, *args], capture_output=True, text=True,
            timeout=timeout, env=_environment(),
        )
    except subprocess.TimeoutExpired:
        return Result(False, "The Obsidian client did not respond in time.")
    except OSError as exc:
        return Result(False, str(exc))

    output = redact((finished.stdout or "") + (finished.stderr or ""))
    # Belt and braces: the arguments themselves must never survive into the
    # output, however the client chose to echo them back.
    for value in args:
        if len(value) > 3 and not value.startswith("--"):
            output = output.replace(value, "<redacted>")
    return Result(finished.returncode == 0, output.strip(), finished.returncode)


def _first_json(text: str):
    """The first JSON value in a mixed stream of log lines and output."""
    for start in (text.find("["), text.find("{")):
        if start == -1:
            continue
        decoder = json.JSONDecoder()
        try:
            value, _ = decoder.raw_decode(text[start:])
            return value
        except ValueError:
            continue
    return None


# --- The operations the web interface needs -----------------------------------


def login(email: str, password: str, mfa: str = "") -> Result:
    """Sign in to Obsidian.

    The MFA code is only sent when there is one: passing an empty ``--mfa`` to
    an account that does not use two-factor is rejected by the client, so an
    account without it would be unable to sign in at all.
    """
    args = ["login", "--email", email, "--password", password]
    if mfa.strip():
        args += ["--mfa", mfa.strip()]
    return run_secret(args)


def logout() -> Result:
    return run(["logout"])


def logged_in() -> bool:
    """Whether an account is signed in, judged without prompting for anything."""
    result = run(["sync-list-remote", "--json"], timeout=30)
    return result.ok


@dataclass
class RemoteVault:
    vault_id: str
    name: str
    raw: dict = field(default_factory=dict)


def list_vaults() -> tuple[list[RemoteVault], Result]:
    """The vaults this account can sync.

    The shape of the JSON is not a contract -- the CLI is at 0.0.x -- so the id
    and name are looked for under several plausible keys rather than one, and a
    vault whose id cannot be found is skipped rather than crashing the page.
    """
    result = run(["sync-list-remote", "--json"], timeout=45)
    if not result.ok:
        return [], result

    rows = result.data
    if isinstance(rows, dict):
        for key in ("vaults", "items", "data", "results"):
            if isinstance(rows.get(key), list):
                rows = rows[key]
                break
    if not isinstance(rows, list):
        return [], result

    vaults: list[RemoteVault] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        vault_id = str(row.get("id") or row.get("vaultId") or row.get("uid") or "")
        name = str(row.get("name") or row.get("vaultName") or vault_id)
        if vault_id:
            vaults.append(RemoteVault(vault_id, name, row))
    return vaults, result


def vault_path(name: str):
    """Where a named vault is written. Kept inside the data volume."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "vault"
    return OBSIDIAN_VAULTS_DIR / safe


def setup(vault_id: str, name: str, encryption_password: str = "") -> Result:
    """Connect a remote vault to a local directory, read-only.

    The mode is the safeguard, and it is the CLI's own rather than a promise
    made here: ``mirror-remote`` downloads and *reverts local changes*, so even
    a bug in Task Hub that wrote into the vault directory would be undone and
    could never reach the real vault. Turning write-back on later is a visible
    change of this one setting, not a checkbox buried in a form.
    """
    path = vault_path(name)
    path.mkdir(parents=True, exist_ok=True)

    args = ["sync-setup", "--vault", vault_id, "--path", str(path),
            "--device-name", "Task Hub", "--json"]
    if encryption_password:
        args += ["--password", encryption_password]
    result = run_secret(args, timeout=DEFAULT_TIMEOUT)
    if not result.ok:
        return result
    return configure(path)


#: Plugin settings Task Hub has to read to do its job: the Tasks plugin's global
#: filter, and TaskNotes' field mapping. Without these the vault arrives with an
#: empty .obsidian directory and both have to be guessed at -- and guessing a
#: renamed field means silently losing it.
#:
#: The *data* only, not "community-plugin" alongside it. That category carries
#: the plugins themselves -- every plugin's main.js and stylesheet -- which came
#: to 33 MB against 0.7 MB of actual notes on a real vault here. Task Hub reads
#: two data.json files and never executes a line of plugin code, so downloading
#: it was paying for something that could not be used.
CONFIG_CATEGORIES = "community-plugin-data"

#: Attachment types to download. Task Hub reads markdown and nothing else, so
#: every PDF, image and video it fetches is storage spent on a file it will
#: never open -- and that is not a rounding error: a real vault of 227 notes
#: measured 1.2 MB of markdown against 866 MB of PDFs.
#:
#: There is no value meaning "none". Passing an empty string *deletes* the
#: setting, which falls back to the client's default of everything; asking for
#: "unsupported" is the narrowest thing it accepts, and it excludes images,
#: audio, video and PDFs, which is the whole of the weight.
FILE_TYPES = "unsupported"


def configure(path) -> Result:
    """Set the sync mode and the config categories on an already-linked vault.

    Split out from :func:`setup` so an existing installation can be brought up
    to date without being unlinked and set up again.
    """
    return run(["sync-config", "--path", str(path),
                "--mode", "mirror-remote",
                "--configs", CONFIG_CATEGORIES,
                "--file-types", FILE_TYPES,
                "--json"], timeout=45)


def set_excluded_folders(path, folders: list[str]) -> Result:
    """Stop syncing whole top-level folders of a vault.

    The blunt instrument, and the effective one. ``--file-types`` already keeps
    images, audio, video and PDFs out, but its narrowest setting is
    "unsupported", which is the bucket every binary an Obsidian plugin invents
    falls into -- a real vault here arrived with 312 MB of Supernote notebooks
    against 430 KB of markdown, all of it "unsupported" and none of it readable.

    Excluding the folder is what actually helps, and it costs nothing: Task Hub
    reads markdown, so a folder holding no markdown holds nothing for it.

    Already-downloaded files are left where they are; the client stops syncing
    the folder rather than tidying up after itself.
    """
    return run(["sync-config", "--path", str(path),
                "--excluded-folders", ",".join(folders), "--json"], timeout=45)


def excluded_folders(vault_id: str) -> list[str]:
    """What the client is currently set to skip, read from its own config.

    Straight from ``sync/<vault id>/config.json`` rather than by parsing the
    CLI's JSON output, which wraps its payload differently between commands.
    The file is the client's own state, so it cannot disagree with itself.
    """
    import json

    path = OBSIDIAN_CONFIG_DIR / "obsidian-headless" / "sync" / vault_id / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    folders = data.get("ignoreFolders") or data.get("excludedFolders") or []
    return sorted(f for f in folders if isinstance(f, str))


def set_sync_mode(path, mode: str) -> Result:
    """Change a linked vault's sync mode.

    The only supported values are ``mirror-remote`` (download, and revert local
    changes) and ``bidirectional``. This is the switch that actually decides
    whether Task Hub can write to a vault: everything else is bookkeeping.
    """
    if mode not in ("mirror-remote", "pull-only", "bidirectional"):
        return Result(False, f"{mode!r} is not a sync mode.")
    return run(["sync-config", "--path", str(path), "--mode", mode, "--json"], timeout=45)


def sync_once(name: str) -> Result:
    return run(["sync", "--path", str(vault_path(name))], timeout=600)


def status(name: str) -> Result:
    return run(["sync-status", "--path", str(vault_path(name)), "--json"], timeout=45)
