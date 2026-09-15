"""Reading tasks out of a synced Obsidian vault.

The vault is already on disk: Obsidian's own headless client keeps a copy in the
data volume, and :mod:`app.services.obsidian_cli` drives it. This connector's
job is only to walk that copy and say what it found, so it does no network calls
at all and cannot fail because a service is down.

**Read-only, and structurally so.** ``create``, ``update`` and ``delete`` refuse
rather than quietly doing nothing, because a silent no-op would let the engine
believe a write succeeded and record a baseline that never existed. Nor is this
the only guard: the client runs in ``mirror-remote`` mode, which reverts local
changes, so nothing written into the vault directory could reach the real vault
even if this file were wrong about its own capabilities.

What a "list" is here is a choice, not something the vault declares. Obsidian
has no lists -- it has files, folders and tags. Folders are what people
recognise as a place, and they nest, so each top-level folder is offered as a
list, alongside the whole vault for anyone who wants everything in one
collection.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from app.connectors.base import (
    F_DUE_DATE,
    F_DUE_TIME,
    F_NOTES,
    F_PRIORITY,
    F_RRULE,
    F_START,
    F_STATUS,
    F_TAGS,
    F_TITLE,
    Capabilities,
    Connector,
    ConnectorError,
    PullResult,
    PushOutcome,
    RemoteItem,
    RemoteList,
)
from app.db.models import CollectionKind, ItemStatus, ServiceKind
from app.services import obsidian_cli as cli
from app.services.obsidian_md import (
    TaskNotesConfig,
    content_fingerprint,
    new_block_id,
    record_to_line,
    record_to_tasknote,
    tasknote_filename,
    looks_like_a_date,
    rewrite_completion,
    verify_only_completion_changed,
    is_task,
    is_tasknote,
    load_tasknotes_config,
    parse_frontmatter,
    parse_line,
    tasknote_project_links,
    stable_id,
    tasknote_to_record,
    to_record,
)

logger = logging.getLogger(__name__)

#: The whole vault as one list. Anything else is "folder:<name>".
WHOLE_VAULT = "vault:"
FOLDER_PREFIX = "folder:"

#: Marks a parent that is still a note *name* rather than a remote id, between
#: reading one TaskNote and resolving the whole pull. Never leaves this module:
#: anything still carrying it at the end of a pull is dropped.
LINK_PREFIX = "link:"

#: Directories never walked. ``.obsidian`` is configuration, ``.trash`` is
#: Obsidian's own recycle bin, and syncing a deleted note back out as a live
#: task is precisely the resurrection this project spends so much effort
#: preventing elsewhere.
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}

#: A vault reporting nothing at all when it held tasks last time is a sync that
#: has not finished, not a vault someone emptied. Same reasoning as the engine's
#: own empty-pull guard: absence is the one signal that destroys data.
MIN_FILES_FOR_TRUST = 1

#: What a note collecting inline tasks is called, when the user has chosen the
#: folder to put it in rather than naming a note themselves.
DEFAULT_INBOX = "From Task Hub.md"

#: The most lines one pass may change. A sync wanting to rewrite more of a
#: vault than this is far more likely to be a fault than an intention, so it
#: stops and says so rather than working through the list.
MAX_WRITES_PER_PASS = 20

#: Fields a task in a vault can carry. Times are included even though a task
#: written on a line cannot hold one, because a TaskNotes file can -- and each
#: item narrows this to what it actually carries through ``fields_present``.
#: Declaring no time support at all would be wrong in the other direction: it
#: would make TaskNotes' times invisible.
VAULT_FIELDS = frozenset({
    F_TITLE, F_NOTES, F_STATUS, F_DUE_DATE, F_DUE_TIME,
    F_START, F_PRIORITY, F_TAGS, F_RRULE,
})

#: What an inline task can actually express. No time, no timezone: there is
#: nowhere in "📅 2026-09-12" to put half past two. Reported per item so that a
#: line read out of a vault can never clear a time set in Todoist.
INLINE_FIELDS = VAULT_FIELDS - {F_DUE_TIME}


class ObsidianConnector(Connector):
    service = ServiceKind.OBSIDIAN
    name = "Obsidian"

    def __init__(self, account_id: int, credentials: dict, sync_state: dict | None = None):
        super().__init__(account_id, credentials, sync_state)
        self.vault_name: str = (credentials or {}).get("name") or ""
        #: Off unless deliberately turned on for this vault, and read fresh from
        #: the account every time a connector is built, so switching it off
        #: takes effect on the very next pass.
        self.write_back: bool = bool((credentials or {}).get("write_back"))
        #: Folders write-back is allowed into. Empty means the whole vault.
        self.write_folders: set[str] = set((credentials or {}).get("write_folders") or [])
        #: Where new tasks are stored, relative to the vault root. Its meaning
        #: follows the format: a *note* for checklist lines, which are appended
        #: to it, and a *folder* for task notes, which each get a file in it.
        #:
        #: Empty means different things for the same reason. TaskNotes already
        #: says where tasks live, so an empty value falls back to that. A
        #: checklist line has nowhere to go, so an empty value leaves creation
        #: off -- a task arriving from Google has no natural home in somebody's
        #: prose, and picking a note on their behalf is how writing ends up in
        #: places nobody expected.
        self.create_note: str = str((credentials or {}).get("create_note") or "").strip()
        #: Which format a new task is written in: "tasknotes", "inline", or
        #: "auto" to let the vault decide. Only meaningful when both plugins are
        #: installed, which is the case this exists for -- a vault with one of
        #: them has no choice to make.
        self.create_format: str = str(
            (credentials or {}).get("create_format") or "auto").strip().lower()
        self._written = 0
        self._settings_cache: tuple[str, TaskNotesConfig] | None = None

    # -- Where the vault is ---------------------------------------------------

    @property
    def root(self) -> Path:
        if not self.vault_name:
            raise ConnectorError("No Obsidian vault has been linked yet.")
        return cli.vault_path(self.vault_name)

    def _require_vault(self) -> Path:
        root = self.root
        if not root.exists():
            raise ConnectorError(
                f"The vault {self.vault_name!r} has not been downloaded yet. "
                "Open Services -> Obsidian and use 'Download now'."
            )
        return root

    # -- Capabilities ---------------------------------------------------------

    def capabilities(self, kind: CollectionKind) -> Capabilities:
        # Deleting stays off whatever else is enabled: removing a line from
        # somebody's note is a different order of change from ticking a box that
        # is already there, and a task completed elsewhere is better marked done
        # in the vault than silently cut out of it.
        #
        # Creating is off unless write-back is on *and* a note has been named to
        # put new tasks in. Both are required, because the dangerous half of
        # creation is not the writing, it is choosing where.
        # A task note needs no destination setting -- the plugin already says
        # where tasks live. An inline task has nowhere to go until a note is
        # named, so for that format the setting is still required.
        can_create = bool(self.write_back) and (
            self.writes_format() == "tasknotes" or bool(self.create_note)
        )
        return Capabilities(
            fields=VAULT_FIELDS,
            can_create=can_create,
            can_delete=False,
            # A vault expresses nesting by indenting a line under another, which
            # is read here. Never written: this connector only ever patches a
            # completion into somebody's notes, and re-indenting their file is
            # not something to do on their behalf.
            supports_parent=True,
            stores_uid=False,
            carries_origin=False,
            writable_fields=frozenset({F_STATUS}) if self.write_back else frozenset(),
        )

    def supports_kind(self, kind: CollectionKind) -> bool:
        # A vault holds tasks, not calendar events. Offering it for calendars
        # would put an empty list in front of the user for them to wonder about.
        return kind == CollectionKind.TASKS

    # -- Discovery ------------------------------------------------------------

    def verify(self) -> str:
        root = self._require_vault()
        return f"{self.vault_name} ({sum(1 for _ in self._markdown_files(root))} notes)"

    def list_remote_lists(self) -> list[RemoteList]:
        """The whole vault, and each top-level folder that holds notes.

        A vault with write-back switched off reports every list as read-only,
        which is what stops the mapping table offering a write tick that could
        never do anything. With write-back on the tick becomes real, so the flag
        follows the setting rather than being hardcoded -- and because this is
        only refreshed on discovery, turning write-back on re-runs discovery.
        """
        root = self._require_vault()
        read_only = not self.write_back

        lists = [RemoteList(
            remote_id=WHOLE_VAULT,
            name="Whole vault",
            kind=CollectionKind.TASKS,
            is_default=True,
            read_only=read_only,
        )]

        folders: set[str] = set()
        for path in self._markdown_files(root):
            relative = path.relative_to(root)
            if len(relative.parts) > 1:
                folders.add(relative.parts[0])

        for folder in sorted(folders, key=str.lower):
            lists.append(RemoteList(
                remote_id=f"{FOLDER_PREFIX}{folder}",
                name=folder,
                kind=CollectionKind.TASKS,
                read_only=read_only,
            ))
        return lists

    # -- Reading --------------------------------------------------------------

    def pull(
        self,
        remote_list_id: str,
        kind: CollectionKind,
        since: dt.datetime | None = None,
        state: dict | None = None,
    ) -> PullResult:
        if kind != CollectionKind.TASKS:
            return PullResult(items=[], incremental=False)

        root = self._require_vault()

        # A pass in flight is a directory that matches no real state of the
        # vault: files arrive one at a time, so a task's note can be genuinely
        # absent for a moment. Reading through that would look exactly like the
        # user having deleted it. Wait for the supervisor to say it has settled
        # and, if it has not, report this pull as incremental so absence is
        # never read as deletion.
        from app.services.obsidian_sync import manager as sync_manager

        if not sync_manager.settled(self.vault_name):
            return PullResult(
                items=[],
                incremental=True,
                errors=["The vault is still downloading; skipping this pass."],
            )

        global_filter, tasknotes = self._settings(root)

        # Which lines count as tasks is the vault's decision, not ours, and it
        # can change under us: someone sets a Tasks-plugin global filter, or
        # renames it from #task to #todo. Every line that no longer matches then
        # vanishes from this pull -- which is indistinguishable from the user
        # having deleted them, and would tombstone them out of Todoist, Google
        # and everywhere else.
        #
        # So the rules are fingerprinted. When they differ from last time this
        # pull is reported as incremental, which stops absence being read as
        # deletion for exactly the one pass where absence means "the rule
        # changed". The pass after that compares like with like and normal
        # deletion detection resumes.
        rules = f"{global_filter}\u0000{tasknotes.task_tag}"
        previous_rules = (state or {}).get("rules")
        rules_changed = previous_rules is not None and previous_rules != rules
        if rules_changed:
            logger.warning(
                "The task rules for %r changed (%r -> %r); this pass will not "
                "treat anything as deleted.",
                self.vault_name, previous_rules, rules,
            )

        scope = self._scope(root, remote_list_id)
        if scope is None:
            return PullResult(
                items=[],
                errors=[f"The folder {remote_list_id!r} is no longer in the vault."],
            )

        items: list[RemoteItem] = []
        errors: list[str] = []
        warnings: list[str] = []
        files = 0

        for path in self._markdown_files(scope):
            files += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                # One unreadable note must not cost the other nine hundred.
                errors.append(f"Could not read {path.name}: {exc}")
                continue

            relative = str(path.relative_to(root)).replace("\\", "/")
            items.extend(
                self._tasks_in(text, relative, global_filter, tasknotes, warnings)
            )

        # Now that every note has been seen, a project link can be turned into
        # the parent's actual remote id. A link pointing at something that is
        # not a task in this pull is left unresolved and cleared: a project can
        # perfectly well be an ordinary note, and claiming a parent that the
        # engine cannot find would strand the child.
        by_name: dict[str, str] = {}
        for item in items:
            if not item.remote_id.startswith("note:"):
                continue
            name = item.remote_id[len("note:"):].rsplit("/", 1)[-1].removesuffix(".md")
            by_name.setdefault(name.lower(), item.remote_id)

        for item in items:
            parent = item.record.parent_remote_id or ""
            if not parent.startswith(LINK_PREFIX):
                continue
            wanted = parent[len(LINK_PREFIX):].strip().lower()
            item.record.parent_remote_id = by_name.get(wanted)

        # A pull that walked no files at all is a vault that has not finished
        # downloading, not a vault with nothing in it. Reporting it as complete
        # would let the engine read every task's absence as a deletion and
        # remove them from every other service.
        complete = files >= MIN_FILES_FOR_TRUST
        if not complete:
            errors.append(
                "No notes were found in the vault. Treating this as a download "
                "that has not finished rather than an emptied vault."
            )

        # Reported, but capped: one malformed date is worth saying, four
        # hundred would bury everything else in the run.
        if warnings:
            errors.extend(warnings[:5])
            if len(warnings) > 5:
                errors.append(
                    f"…and {len(warnings) - 5} more task(s) with a date Obsidian's "
                    "format does not recognise."
                )

        if rules_changed:
            errors.append(
                "What counts as a task in this vault changed since the last "
                "sync, so nothing was treated as deleted this time. Tasks that "
                "no longer qualify are left alone in your other services."
            )

        return PullResult(
            items=items,
            incremental=not complete or rules_changed,
            errors=errors,
            sync_state={"rules": rules},
        )

    def _tasks_in(
        self,
        text: str,
        relative: str,
        global_filter: str,
        tasknotes: TaskNotesConfig,
        warnings: list[str] | None = None,
    ) -> list[RemoteItem]:
        """Every task one note contributes: itself, or the lines inside it."""
        front, body = parse_frontmatter(text)

        if front and is_tasknote(front, tasknotes):
            record = tasknote_to_record(
                front, uid="", vault_name=self.vault_name,
                relative_path=relative, config=tasknotes, body=body,
            )
            # TaskNotes expresses containment by pointing at another note, and
            # a name is not a remote id. It is recorded provisionally here and
            # resolved once the whole pull is in hand, because the note being
            # pointed at may not have been walked yet -- and may not be a task
            # at all, in which case it is a project label rather than a parent.
            links = tasknote_project_links(front, tasknotes)
            if links:
                record.parent_remote_id = f"{LINK_PREFIX}{links[0]}"

            # A TaskNotes file IS the task, so the file is its identity -- no
            # hashing, and it survives the title being rewritten.
            return [RemoteItem(
                remote_id=f"note:{relative}",
                record=record,
                # Its dates are timestamps, so a time is genuinely expressible.
                fields_present=VAULT_FIELDS,
                remote_updated_at=None,
            )]

        found: list[RemoteItem] = []
        #: Open ancestors while walking one file, as (indent width, remote id).
        #: Reset per file, because nesting never spans one.
        stack: list[tuple[int, str]] = []
        #: The fence that opened the code block currently being skipped, if any.
        #:
        #: A checklist line inside a code block is an example of a task, not a
        #: task. Obsidian's own Tasks plugin ignores them, so reading them makes
        #: Task Hub's list disagree with the one the user sees -- and the notes
        #: most likely to contain them are the ones explaining how tasks work,
        #: which every vault that uses the plugin tends to have.
        #:
        #: Only fenced blocks are skipped. An indented code block cannot be told
        #: apart from an indented subtask without tracking blank lines and list
        #: context, and getting that wrong would silently drop real subtasks --
        #: a worse failure than the one being fixed.
        fence: str | None = None
        for number, line in enumerate(text.splitlines()):
            stripped = line.lstrip()
            if fence is not None:
                if stripped.startswith(fence):
                    fence = None
                continue
            if stripped.startswith("```") or stripped.startswith("~~~"):
                # A fence may be longer than three characters and is closed only
                # by one at least as long, so the opener's own length is kept.
                run = len(stripped) - len(stripped.lstrip(stripped[0]))
                fence = stripped[0] * max(run, 3)
                continue

            task = parse_line(line, number)
            if task is None or not is_task(task, global_filter):
                continue

            # Indentation is how a vault says one task belongs to another, so
            # the parent is the nearest task above it that is less indented.
            #
            # Only tasks are considered, never plain checklist lines. A vault is
            # full of shopping lists and packing lists, and treating one of
            # those as the parent of a real task would invent a hierarchy the
            # author never wrote.
            width = len(task.indent.expandtabs(4))
            while stack and stack[-1][0] >= width:
                stack.pop()
            parent_remote = stack[-1][1] if stack else None
            this_remote = f"{relative}#{stable_id(relative, task)}"
            stack.append((width, this_remote))
            record = to_record(
                task, uid="", vault_name=self.vault_name,
                relative_path=relative, global_filter=global_filter,
            )
            # A date written the wrong way round is worse than no date: the
            # task syncs looking as though it never had a deadline, and there
            # is nothing on the page to say one was dropped.
            if warnings is not None and not record.title.strip():
                # A task with no words is not a task anybody can act on, and it
                # arrives in Google and Todoist as a blank row. Said out loud
                # rather than invented a name for: only the author knows what it
                # was meant to say.
                warnings.append(
                    f"{relative}: a task on line {task.line_number + 1} has no "
                    "description, so it will appear with no name everywhere it "
                    "syncs."
                )
            if warnings is not None:
                for field in ("due", "scheduled", "start"):
                    written = task.value(field) or ""
                    if looks_like_a_date(written):
                        warnings.append(
                            f"{relative}: {task.description[:40]!r} has "
                            f"{field} “{written}”, which Obsidian's task format "
                            "does not recognise. Dates must be written "
                            "YYYY-MM-DD, for example 2026-09-10."
                        )
            record.parent_remote_id = parent_remote
            found.append(RemoteItem(
                remote_id=this_remote,
                record=record,
                # A line cannot hold a time. Saying so per item is what stops it
                # appearing to erase a 2:30pm that Todoist is holding.
                fields_present=INLINE_FIELDS,
                # The file's modification time is useless here: editing any line
                # in a daily note would make every task in it look freshly
                # edited. The engine compares content instead.
                remote_updated_at=None,
                etag=content_fingerprint(task),
            ))
        return found

    # -- Writing: refused, loudly ---------------------------------------------

    def writes_format(self, root: Path | None = None) -> str:
        """Which format this vault's new tasks are written in.

        A vault with TaskNotes installed can hold a time of day and a
        description; an inline task cannot. So when the user has expressed no
        preference and both are available, TaskNotes wins -- it loses less of
        the task. When they have expressed one, it is obeyed even where the
        other would carry more, because a vault is somebody's own filing system
        and Task Hub's opinion about formats does not outrank theirs.
        """
        if (self.create_format or "auto") in ("inline", "tasknotes"):
            return self.create_format
        # Asked before a vault is linked -- from the capability check, which must
        # never raise. Inline is the answer that needs no plugin installed.
        try:
            root = root or self.root
        except ConnectorError:
            return "inline"
        tasknotes = root / ".obsidian" / "plugins" / "tasknotes" / "data.json"
        return "tasknotes" if tasknotes.is_file() else "inline"

    def _parent_note_name(self, record) -> str:
        """The note a new task should name as its project, if it has a parent.

        The engine fills ``parent_remote_id`` with the parent's id *at this
        service*, which for a task note is ``note:<path>``. Only that form can
        become a wikilink; a parent held as an inline task cannot be pointed at
        from frontmatter, so it is dropped rather than guessed at.
        """
        parent = getattr(record, "parent_remote_id", "") or ""
        if not parent.startswith("note:"):
            return ""
        return parent[len("note:"):].rsplit("/", 1)[-1].removesuffix(".md")

    def create(self, remote_list_id, record, kind) -> PushOutcome:
        """Write a task from another service into the vault.

        Two shapes, chosen by :meth:`writes_format`: a whole task note, or a
        line appended to a nominated note. They fail differently and are kept
        apart for that reason -- a new file cannot damage anything, while an
        appended line is written into text somebody else owns.
        """
        if not self.write_back:
            return PushOutcome(remote_id=None, error=self._read_only())
        if self._written >= MAX_WRITES_PER_PASS:
            return PushOutcome(
                remote_id=None,
                error=(
                    f"Stopped after {MAX_WRITES_PER_PASS} changes in one pass. "
                    "A sync that wants to rewrite more of your vault than that "
                    "is more likely to be a fault than an intention."
                ),
            )
        root = self._require_vault()
        if self.writes_format(root) == "tasknotes":
            return self._create_tasknote(root, remote_list_id, record)
        return self._create_inline(root, remote_list_id, record)

    def _create_tasknote(self, root: Path, remote_list_id: str, record) -> PushOutcome:
        """Write one whole task note, the way the TaskNotes plugin would.

        Nothing existing is touched: the task gets a file of its own, in the
        folder the plugin itself puts tasks in, named the way it names them. If
        anything about it is wrong the file is simply removed again.
        """
        _global_filter, tasknotes = self._settings(root)

        folder = (self.create_note or tasknotes.tasks_folder or "").strip().strip("/")
        stem = tasknote_filename(record.title or "")
        relative = f"{folder}/{stem}.md" if folder else f"{stem}.md"

        if not self._may_write(remote_list_id, relative):
            return PushOutcome(
                remote_id=None,
                error="Write-back is not enabled for the folder new tasks go in.",
            )

        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            return PushOutcome(remote_id=None, error="That folder is outside the vault.")

        # A name already taken belongs to somebody else's note, which must not
        # be overwritten -- and two tasks really can share a title.
        if path.exists():
            for suffix in range(2, 50):
                candidate = f"{folder}/{stem} {suffix}.md" if folder else f"{stem} {suffix}.md"
                if not (root / candidate).exists():
                    relative, path = candidate, (root / candidate).resolve()
                    break
            else:
                return PushOutcome(
                    remote_id=None,
                    error=f"Could not find an unused name for {stem!r} in {folder!r}.",
                )

        text = record_to_tasknote(
            record, config=tasknotes, parent_note=self._parent_note_name(record))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written_back = path.read_text(encoding="utf-8")
        except OSError as exc:
            return PushOutcome(remote_id=None, error=f"Could not write {relative}: {exc}")

        if written_back != text:
            self._remove(path, relative)
            return PushOutcome(
                remote_id=None,
                error=f"{relative} did not save as expected and was removed.",
            )

        # The same refusal as the inline path, for the same reason: a task that
        # cannot be read back is written again on every pass, for ever.
        front, _body = parse_frontmatter(written_back)
        if not front or not is_tasknote(front, tasknotes):
            self._remove(path, relative)
            return PushOutcome(
                remote_id=None,
                error=(
                    f"The note written for {record.title!r} would not read back "
                    "as a task, so it was removed rather than left to be written "
                    "again on every pass."
                ),
            )

        self._written += 1
        logger.info("Created task note %s", relative)
        return PushOutcome(remote_id=f"note:{relative}")

    @staticmethod
    def _remove(path, relative: str) -> None:
        try:
            path.unlink(missing_ok=True)
            logger.warning("Removed %s, which had just been created", relative)
        except OSError:
            logger.exception("Could not remove %s after a failed write", relative)

    def _create_inline(self, root: Path, remote_list_id, record) -> PushOutcome:
        """Append a task from another service to the vault's chosen note.

        Creating is the one write here that does not touch a line somebody else
        wrote: the task is added at the end of a note nominated for exactly this
        purpose, so the worst case is an unwanted line in a file whose whole job
        is to collect them. That is why creation is allowed at all while
        rewriting an existing task's text still is not.

        The note is made if it does not exist, with a sentence at the top saying
        where its contents come from, because a file appearing in somebody's
        vault with no explanation is its own kind of damage.
        """
        if not self.create_note:
            return PushOutcome(
                remote_id=None,
                error=(
                    "No note has been chosen for new tasks in this vault, so "
                    "there is nowhere to put this one."
                ),
            )

        # The setting is a folder, chosen from the vault rather than typed, so
        # the note itself is named here. A value that already names a note is
        # honoured as one: that is what earlier versions stored, and silently
        # treating it as a folder would start writing somewhere else.
        relative = self.create_note
        if not relative.lower().endswith(".md"):
            relative = f"{relative.strip('/')}/{DEFAULT_INBOX}" if relative.strip("/") \
                else DEFAULT_INBOX
        if not self._may_write(remote_list_id, relative):
            return PushOutcome(
                remote_id=None,
                error="Write-back is not enabled for the folder that note is in.",
            )

        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            return PushOutcome(remote_id=None, error="That note is outside the vault.")

        existed = path.is_file()
        try:
            original = path.read_text(encoding="utf-8") if existed else ""
        except OSError as exc:
            return PushOutcome(remote_id=None, error=f"Could not read {relative}: {exc}")

        global_filter, _ = self._settings(root)
        block_id = new_block_id()
        line = record_to_line(record, block_id=block_id, global_filter=global_filter)

        if existed:
            body = original
            if body and not body.endswith("\n"):
                body += "\n"
            updated = body + line + "\n"
        else:
            updated = (
                f"# {path.stem}\n"
                "\n"
                "Tasks synced into this vault by Task Hub. Edit or move them "
                "freely; the anchor at the end of each line is how they are "
                "recognised afterwards.\n"
                "\n"
                + line + "\n"
            )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(updated, encoding="utf-8")
            written_back = path.read_text(encoding="utf-8")
        except OSError as exc:
            return PushOutcome(remote_id=None, error=f"Could not write {relative}: {exc}")

        # Checked against what is on disk, not against what was intended, and
        # checked for the two things that matter: the task can be read back, and
        # nothing that was already in the note has moved.
        if written_back != updated:
            self._restore_or_remove(path, original, relative, existed)
            return PushOutcome(
                remote_id=None,
                error=f"{relative} did not save as expected and was put back.",
            )

        if existed:
            before = original.splitlines()
            after = written_back.splitlines()
            if after[: len(before)] != before:
                self._restore_or_remove(path, original, relative, existed)
                return PushOutcome(
                    remote_id=None,
                    error=f"Writing to {relative} would have changed another line; it was put back.",
                )

        written = parse_line(line, 0)
        if written is None or not is_task(written, global_filter):
            # Refusing here is the important one. A line that cannot be read
            # back is not merely lost: the next pass would find the task absent
            # from the vault and write it in again, once per pass, for ever.
            self._restore_or_remove(path, original, relative, existed)
            return PushOutcome(
                remote_id=None,
                error=(
                    f"The line written for {record.title!r} could not be read "
                    "back as a task, so it was removed rather than left to be "
                    "written again on every pass."
                ),
            )

        self._written += 1
        logger.info("Added %r to %s", (record.title or "")[:40], relative)
        return PushOutcome(remote_id=f"{relative}#{stable_id(relative, written)}")

    @staticmethod
    def _restore_or_remove(path, original: str, relative: str, existed: bool) -> None:
        """Undo a failed creation, whether or not the note was there before."""
        if existed:
            ObsidianConnector._restore(path, original, relative)
            return
        try:
            path.unlink(missing_ok=True)
            logger.warning("Removed %s, which had just been created", relative)
        except OSError:
            logger.exception("Could not remove %s after a failed write", relative)

    def update(self, remote_list_id, remote_id, record, kind) -> PushOutcome:
        """Tick a task off in the vault, or un-tick it. Nothing else.

        Every step here exists because the alternative is damaging somebody's
        notes: the line is patched rather than rebuilt, the file is written only
        after the new line has been checked, it is read back from disk and
        checked again, and anything unexpected puts the original file back.
        """
        if not self.write_back:
            return PushOutcome(remote_id=remote_id, error=self._read_only())
        if not self._may_write(remote_list_id, remote_id):
            return PushOutcome(
                remote_id=remote_id,
                error="Write-back is not enabled for this folder of the vault.",
            )
        if self._written >= MAX_WRITES_PER_PASS:
            return PushOutcome(
                remote_id=remote_id,
                error=(
                    f"Stopped after {MAX_WRITES_PER_PASS} changes in one pass. "
                    "A sync that wants to rewrite more of your vault than that "
                    "is more likely to be a fault than an intention."
                ),
            )

        root = self._require_vault()
        relative, _, _ = remote_id.partition("#")
        path = (root / relative).resolve()
        if root.resolve() not in path.parents and path != root.resolve():
            return PushOutcome(remote_id=remote_id, error="That note is outside the vault.")
        if not path.is_file():
            return PushOutcome(remote_id=remote_id, error=f"{relative} is no longer in the vault.")

        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            return PushOutcome(remote_id=remote_id, error=f"Could not read {relative}: {exc}")

        lines = original.splitlines(keepends=True)
        target = None
        for index, raw in enumerate(lines):
            task = parse_line(raw.rstrip("\r\n"), index)
            if task is None:
                continue
            if f"{relative}#{stable_id(relative, task)}" == remote_id:
                target = (index, raw, task)
                break

        if target is None:
            # The line moved or was edited in Obsidian. Refusing is right: the
            # alternative is guessing which line was meant.
            return PushOutcome(
                remote_id=remote_id,
                error="That task is no longer where it was in the note; it was left alone.",
            )

        index, raw, task = target
        ending = raw[len(raw.rstrip("\r\n")):]
        completed = record.status == ItemStatus.COMPLETED
        done_on = (record.completed_at.date() if record.completed_at else None)

        try:
            patched = rewrite_completion(task, completed, done_on)
        except ValueError as exc:
            return PushOutcome(remote_id=remote_id, error=str(exc))

        if patched == task.raw:
            return PushOutcome(remote_id=remote_id)      # already as it should be

        problem = verify_only_completion_changed(task.raw, patched)
        if problem:
            logger.error("Refusing to write %s: %s", relative, problem)
            return PushOutcome(remote_id=remote_id, error=f"Refused to write {relative}: {problem}")

        lines[index] = patched + ending
        updated = "".join(lines)

        try:
            path.write_text(updated, encoding="utf-8")
            written_back = path.read_text(encoding="utf-8")
        except OSError as exc:
            return PushOutcome(remote_id=remote_id, error=f"Could not write {relative}: {exc}")

        # Checked against what is on disk, not against what was intended.
        if written_back != updated:
            self._restore(path, original, relative)
            return PushOutcome(
                remote_id=remote_id,
                error=f"{relative} did not save as expected and was put back.",
            )
        before_rest = original.splitlines(keepends=True)
        after_rest = written_back.splitlines(keepends=True)
        if len(before_rest) != len(after_rest) or any(
            a != b for i, (a, b) in enumerate(zip(before_rest, after_rest)) if i != index
        ):
            self._restore(path, original, relative)
            return PushOutcome(
                remote_id=remote_id,
                error=f"Writing to {relative} would have changed another line; it was put back.",
            )

        self._written += 1
        logger.info("Marked %r %s in %s", task.description[:40],
                    "complete" if completed else "not complete", relative)
        return PushOutcome(remote_id=remote_id)

    def _may_write(self, remote_list_id: str, remote_id: str) -> bool:
        """Whether write-back is allowed into the folder this note sits in."""
        if not self.write_folders:
            return True
        relative, _, _ = remote_id.partition("#")
        top = relative.split("/", 1)[0] if "/" in relative else ""
        return f"folder:{top}" in self.write_folders or "vault:" in self.write_folders

    @staticmethod
    def _restore(path, original: str, relative: str) -> None:
        try:
            path.write_text(original, encoding="utf-8")
            logger.warning("Put %s back as it was", relative)
        except OSError:
            logger.exception("Could not restore %s after a failed write", relative)

    def delete(self, remote_list_id, remote_id, kind) -> PushOutcome:
        return PushOutcome(remote_id=remote_id, error=self._read_only())

    @staticmethod
    def _read_only() -> str:
        """Said in full rather than as "not supported", because the reason
        matters: this is a deliberate design choice, not a missing feature."""
        return (
            "Task Hub does not write to Obsidian. Your notes are read only. "
            "Obsidian's client also runs in mirror-remote mode, which would "
            "revert any change before it could reach your vault."
        )

    # -- Walking the vault ----------------------------------------------------

    def _scope(self, root: Path, remote_list_id: str) -> Path | None:
        if remote_list_id == WHOLE_VAULT or not remote_list_id:
            return root
        if remote_list_id.startswith(FOLDER_PREFIX):
            folder = root / remote_list_id[len(FOLDER_PREFIX):]
            # Refuse anything that climbs out of the vault, however it was
            # spelled -- a list id is data, and data does not get to choose
            # which directories are read.
            try:
                folder.resolve().relative_to(root.resolve())
            except ValueError:
                return None
            return folder if folder.is_dir() else None
        return None

    @staticmethod
    def _markdown_files(scope: Path):
        """Every note under a directory, skipping Obsidian's own machinery."""
        if not scope.exists():
            return
        for path in sorted(scope.rglob("*.md")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                yield path

    # -- The vault's own settings ---------------------------------------------

    def _settings(self, root: Path) -> tuple[str, TaskNotesConfig]:
        """The Tasks global filter and the TaskNotes field mapping.

        Read from the plugins' own settings rather than assumed. The global
        filter decides what counts as a task at all, and TaskNotes lets every
        property be renamed -- guessing either means silently syncing the wrong
        things, or losing a field without saying so.

        Cached per pull, because this is read once per list and a vault can be
        mapped into several collections.
        """
        key = str(root)
        if self._settings_cache and self._settings_cache[0] == key:
            return self._settings_cache[1]

        plugins = root / ".obsidian" / "plugins"

        global_filter = ""
        data = self._read_json(plugins / "obsidian-tasks-plugin" / "data.json")
        if isinstance(data, dict):
            global_filter = str(data.get("globalFilter") or "").strip()

        tasknotes = load_tasknotes_config(
            self._read_json(plugins / "tasknotes" / "data.json")
        )

        value = (global_filter, tasknotes)
        self._settings_cache = (key, value)
        return value

    @staticmethod
    def _read_json(path: Path):
        """Somebody else's settings file: never let it raise."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
