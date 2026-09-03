"""Reading tasks out of an Obsidian vault's markdown.

Pure parsing: this module never touches the filesystem and never writes. It
turns the text of a vault into canonical records and back, so every rule about
what a task *is* can be tested directly, without a vault, a subscription or a
network.

Three shapes of task exist in the wild and a single vault commonly holds more
than one:

* **Inline tasks** -- a markdown checklist line, optionally carrying metadata
  from the Obsidian Tasks plugin either as emoji (``📅 2026-09-12``) or in
  Dataview's inline-field syntax (``[due:: 2026-09-12]``).
* **TaskNotes** -- one task per file, its fields in YAML frontmatter. Much the
  safer format to round-trip, because the fields are structured rather than
  embedded in prose.
* **Plain checkboxes** -- and these are deliberately *not* tasks. See below.

A note on writing, which this module is shaped for even though nothing calls it
yet. The Tasks plugin parses a line **backwards from the end**, so its metadata
has to appear in a fixed order and anything after it stops the parse dead. That
makes "reconstruct the line from the fields we parsed" a data-destroying
operation: it would drop wikilinks, footnotes, bold text, indentation and any
metadata belonging to a plugin we have never heard of. So the rule here is that
a line is *patched*, never regenerated, and the parser records the exact span of
every token it understands so that a future writer can replace those spans and
leave every other byte alone.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote

from app.db.models import CollectionKind, ItemStatus, ServiceKind
from app.services.ical_model import CanonicalRecord

# --- What counts as a task ----------------------------------------------------
#
# The single most important rule in this file. A vault is full of checklists
# that are not tasks -- shopping lists, packing lists, notes-to-self, the steps
# of a recipe -- and syncing those into Todoist and Google Tasks would be worse
# than useless. So a bare "- [ ] milk" is never a task here.
#
# A line has to be *marked*. Either the vault's global filter appears in it (the
# Tasks plugin's own answer to exactly this problem, usually "#task"), or it
# carries at least one real task field: a date, a priority, a recurrence rule.
# Someone who wrote a due date on a line meant it as a task.
#
# When a vault sets a global filter, that filter alone decides, because it is
# precisely what Obsidian itself shows the user. Anything else would mean Task
# Hub's idea of the task list and Obsidian's disagreeing, which is worse than
# either rule on its own.

#: Markdown list markers that can carry a checkbox: -, *, + and 1. / 1)
_BULLET = r"(?:[-*+]|\d+[.)])"

#: A checklist line: leading whitespace, a bullet, a bracketed status character,
#: then the rest. The status character is captured rather than matched against a
#: fixed set, because users define their own and a character we do not recognise
#: must survive untouched rather than be normalised into one we do.
CHECKBOX_RE = re.compile(
    rf"^(?P<indent>[ \t]*)(?P<bullet>{_BULLET})[ \t]+"
    rf"\[(?P<status>.)\][ \t]+(?P<body>.*)$"
)

#: Status characters the Tasks plugin ships with. Anything else is a custom
#: status: treated as "not done", and preserved exactly as written.
_STATUS_BY_CHAR = {
    " ": ItemStatus.NEEDS_ACTION,
    "x": ItemStatus.COMPLETED,
    "X": ItemStatus.COMPLETED,
    "/": ItemStatus.IN_PROCESS,
    "-": ItemStatus.CANCELLED,
}


# --- Field syntax -------------------------------------------------------------

#: The emoji the Tasks plugin uses, and the canonical field each one names.
#: Kept as a plain mapping rather than a regex alternation so that the writer
#: can look up the emoji for a field as easily as the field for an emoji.
EMOJI_FIELDS: dict[str, str] = {
    "➕": "created",
    "🛫": "start",
    "⏳": "scheduled",
    "📅": "due",
    "✅": "done",
    "❌": "cancelled",
    "🔁": "recurrence",
    "🆔": "id",
    "⛔": "depends_on",
    "🏁": "on_completion",
}

#: Priority is a bare emoji with no value. Six levels, and note that "medium"
#: sits ABOVE normal rather than below it -- normal is the absence of any emoji.
#: Mapped onto iCalendar's 1-9 scale, where 1 is the most important and 0 means
#: no priority was set at all, which is the convention every other connector in
#: Task Hub already uses.
PRIORITY_EMOJI: dict[str, int] = {
    "🔺": 1,   # highest
    "⏫": 3,   # high
    "🔼": 4,   # medium
    "🔽": 6,   # low
    "⏬": 9,   # lowest
}

#: Dataview's inline fields, which the Tasks plugin reads as an alternative to
#: the emoji. The names differ from the emoji names in two places -- "completion"
#: rather than "done", "repeat" rather than "recurrence" -- and getting those
#: wrong silently loses the field, so they are spelled out rather than derived.
DATAVIEW_FIELDS: dict[str, str] = {
    "created": "created",
    "start": "start",
    "scheduled": "scheduled",
    "due": "due",
    "completion": "done",
    "cancelled": "cancelled",
    "repeat": "recurrence",
    "id": "id",
    "dependson": "depends_on",
    "oncompletion": "on_completion",
    "priority": "priority",
}

#: Dataview writes [key:: value]; it also reads (key:: value).
DATAVIEW_RE = re.compile(r"[\[(]\s*([A-Za-z][A-Za-z0-9_-]*)\s*::\s*([^\])]*?)\s*[\])]")

#: Dataview's priority takes words rather than a number.
DATAVIEW_PRIORITY = {
    "highest": 1, "high": 3, "medium": 4, "normal": 0, "low": 6, "lowest": 9,
}

#: A trailing block reference, which is Obsidian's own per-line anchor.
BLOCK_REF_RE = re.compile(r"\s\^(?P<id>[A-Za-z0-9-]+)\s*$")

#: A tag. Deliberately strict about the leading boundary so that a colour like
#: "#fff" inside a code span, or a heading's "#", is not read as a tag.
TAG_RE = re.compile(r"(?:(?<=\s)|(?<=^))#([A-Za-z0-9_/-]*[A-Za-z_/-][A-Za-z0-9_/-]*)")

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: A value that was clearly meant as a date but is not written the way the
#: Tasks plugin defines. Used only to warn: guessing between 9/10/26 as the
#: ninth of October and the tenth of September is exactly the sort of silent
#: month/day swap this project refuses to make, so an unreadable date is
#: reported rather than interpreted.
_DATEISH_RE = re.compile(r"^\s*\d{1,4}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{1,4}\s*$")


def looks_like_a_date(raw: str) -> bool:
    """Whether a value was meant as a date but cannot be read as one."""
    raw = (raw or "").strip()
    return bool(raw) and _DATE_RE.match(raw) is None and bool(_DATEISH_RE.match(raw))

#: Fields whose value is a date. Everything else is text.
_DATE_FIELDS = {"created", "start", "scheduled", "due", "done", "cancelled"}


def _parse_date(raw: str) -> dt.date | None:
    """A bare YYYY-MM-DD, or nothing.

    Obsidian task dates carry no time and no timezone at all, which is a real
    limit of the format rather than an omission here: there is nowhere in the
    line to put "2:30pm". Anything that is not exactly a date is left alone
    rather than guessed at.
    """
    match = _DATE_RE.match(raw.strip())
    if not match:
        return None
    try:
        return dt.date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:      # 2026-02-31 and friends
        return None


# --- The parsed shape ---------------------------------------------------------


@dataclass
class Token:
    """One piece of metadata, and exactly where it sat in the line.

    The span is what makes a non-destructive write possible later: a writer can
    replace the bytes between ``start`` and ``end`` and leave the rest of the
    line -- the user's wikilinks, emphasis, footnotes and anything belonging to
    a plugin nobody here has heard of -- untouched.
    """

    field: str
    value: str
    start: int
    end: int
    #: "emoji" or "dataview". A vault settles on one and a writer must follow
    #: it; rewriting a user's whole vault into the other format is not ours to do.
    syntax: str


@dataclass
class InlineTask:
    """A checklist line that qualified as a task."""

    line_number: int                    # 0-based, within the file
    raw: str                            # the line exactly as written
    indent: str
    bullet: str
    status_char: str
    description: str                    # the body with metadata removed
    tokens: list[Token] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    block_id: str | None = None
    priority: int = 0
    syntax: str = "emoji"
    #: Absolute positions within ``raw``. Token spans are relative to the body,
    #: so a writer needs these to patch the line the user actually wrote --
    #: which is the whole point of recording spans in the first place.
    status_index: int = -1
    body_offset: int = 0
    body_end: int = 0

    def value(self, name: str) -> str | None:
        for token in self.tokens:
            if token.field == name:
                return token.value
        return None

    @property
    def status(self) -> ItemStatus:
        return _STATUS_BY_CHAR.get(self.status_char, ItemStatus.NEEDS_ACTION)


# --- Inline parsing -----------------------------------------------------------


def _strip_spans(body: str, spans: list[tuple[int, int]]) -> str:
    """Remove the given spans from a string and tidy the whitespace left over."""
    kept, cursor = [], 0
    for start, end in sorted(spans):
        kept.append(body[cursor:start])
        cursor = max(cursor, end)
    kept.append(body[cursor:])
    return re.sub(r"\s{2,}", " ", "".join(kept)).strip()


def parse_line(line: str, line_number: int = 0) -> InlineTask | None:
    """Parse one line as a checklist item, or return None if it is not one.

    Returns a task for *any* checklist line, whether or not it qualifies as
    something to sync. Deciding that is :func:`is_task`'s job, and keeping the
    two apart means the rule can be changed without touching the parsing, and
    tested against lines that deliberately fail it.
    """
    match = CHECKBOX_RE.match(line)
    if not match:
        return None

    body = match["body"]

    # The block reference comes off first. It always sits at the very end of the
    # line, after the metadata, so leaving it in place would let the last emoji
    # field swallow it -- and a due date read as "2026-09-12 ^a1b2c3" is not a
    # date at all, so the task would silently lose its deadline.
    block_id = None
    block = BLOCK_REF_RE.search(body)
    if block:
        block_id = block["id"]
        body = body[: block.start()].rstrip()

    spans: list[tuple[int, int]] = []
    tokens: list[Token] = []
    priority = 0
    seen_dataview = False

    # Dataview fields first: they are unambiguously delimited, so taking them
    # out of the way cannot disturb the emoji scan that follows.
    for found in DATAVIEW_RE.finditer(body):
        name = DATAVIEW_FIELDS.get(found[1].strip().lower())
        if name is None:
            continue        # someone else's inline field; leave it in place
        seen_dataview = True
        if name == "priority":
            priority = DATAVIEW_PRIORITY.get(found[2].strip().lower(), 0)
        else:
            tokens.append(
                Token(name, found[2].strip(), found.start(), found.end(), "dataview")
            )
        spans.append((found.start(), found.end()))

    # Then the emoji. Each one owns everything up to the next emoji or the end
    # of the line, which is how the Tasks plugin itself reads them.
    emoji_positions: list[tuple[int, str]] = []
    for index, char in enumerate(body):
        if char in EMOJI_FIELDS or char in PRIORITY_EMOJI:
            emoji_positions.append((index, char))

    for position, (index, char) in enumerate(emoji_positions):
        stop = emoji_positions[position + 1][0] if position + 1 < len(emoji_positions) else len(body)
        if char in PRIORITY_EMOJI:
            priority = PRIORITY_EMOJI[char]
            spans.append((index, index + len(char)))
            continue
        name = EMOJI_FIELDS[char]
        value_at = index + len(char)
        raw_value = body[value_at:stop]
        value, end = raw_value.strip(), stop

        # A date field owns exactly one date and nothing else. Tags are allowed
        # to trail the metadata, so without this a "📅 2026-09-12 #home" would
        # give a due value of "2026-09-12 #home", which parses as no date at all
        # and loses both the deadline and the tag.
        if name in _DATE_FIELDS:
            found = re.match(r"\s*(\d{4}-\d{2}-\d{2})", raw_value)
            if found:
                value, end = found[1], value_at + found.end()

        tokens.append(Token(name, value, index, end, "emoji"))
        spans.append((index, end))

    description = _strip_spans(body, spans)
    tags = TAG_RE.findall(description)

    return InlineTask(
        line_number=line_number,
        raw=line,
        status_index=match.start("status"),
        body_offset=match.start("body"),
        body_end=match.start("body") + len(body),
        indent=match["indent"],
        bullet=match["bullet"],
        status_char=match["status"],
        description=description,
        tokens=tokens,
        tags=tags,
        block_id=block_id,
        priority=priority,
        syntax="dataview" if seen_dataview else "emoji",
    )


#: Fields whose presence marks a line as a real task rather than a checklist
#: item. A date, a priority or a recurrence is somebody saying "this is a task".
#: An id or a dependency alone is not enough -- those appear on checklist items
#: that other plugins have annotated.
TASK_MARKERS = {"due", "scheduled", "start", "recurrence", "done", "cancelled"}


def is_task(task: InlineTask, global_filter: str = "") -> bool:
    """Whether a checklist line should be synced as a task.

    With a global filter configured, that filter alone decides. It is the Tasks
    plugin's own mechanism for this exact problem, so honouring it means Task
    Hub's idea of the task list and Obsidian's are the same list -- and a
    disagreement between those two is worse than any rule on its own.

    Without one, the plugin's default is to treat every checkbox in the vault as
    a task, which is the behaviour this whole module exists to avoid. So the
    line has to carry a real task field instead.
    """
    if global_filter:
        return global_filter in task.raw
    if task.priority:
        return True
    return any(token.field in TASK_MARKERS for token in task.tokens)


# --- Identity -----------------------------------------------------------------


def stable_id(vault_path: str, task: InlineTask) -> str:
    """A durable identifier for one inline task.

    Line numbers are unusable: these are files people edit by hand all day, and
    adding a paragraph at the top would renumber everything below it and orphan
    every link.

    A block reference is used where the user already has one, because it is
    Obsidian's own anchor and survives both edits and file moves. Otherwise the
    identity is derived from the file path and the task's own text, which is
    stable as long as neither changes -- and when one does change, the task is
    re-anchored by matching on the other rather than being deleted and recreated.

    Deliberately not the Tasks plugin's own 🆔 field: that field means "the
    handle other tasks depend on", writing our own values into it would collide
    with the user's dependency graph, and the plugin strips it when a recurring
    task rolls over.
    """
    if task.block_id:
        return f"block:{task.block_id}"
    seed = f"{vault_path}\n{task.description}".encode()
    return "hash:" + hashlib.sha1(seed).hexdigest()[:16]


def content_fingerprint(task: InlineTask) -> str:
    """A hash of the task's own bytes, for telling a real edit from a neighbour's.

    A markdown file has one modification time for the whole file, so editing any
    line in a daily note marks every task in it as freshly modified. Comparing
    this instead means a task whose own text has not changed is reported as
    unchanged, however busy the rest of the file has been.
    """
    return hashlib.sha1(task.raw.strip().encode()).hexdigest()[:16]


# --- Writing back: completion only, by patching ------------------------------

#: The character the Tasks plugin uses for a finished task.
DONE_CHAR = "x"
OPEN_CHAR = " "


def rewrite_completion(
    task: InlineTask, completed: bool, done_on: dt.date | None = None
) -> str:
    """The same line, with only its completion changed.

    Patches the original text rather than rebuilding it from what was parsed.
    That distinction is the whole safety argument: the Tasks plugin reads a line
    backwards from the end and stops at the first thing it does not recognise,
    so a regenerated line silently drops wikilinks, footnotes, block references,
    indentation and any other plugin's metadata. Here, everything outside the
    two spans this function owns is copied through byte for byte.

    The two spans are the status character inside the brackets, and the ``done``
    date token. Nothing else is touched -- not the description, not the due
    date, not the tags.
    """
    line = task.raw

    # 1. The status character. Exactly one character, at a known index.
    if task.status_index < 0 or task.status_index >= len(line):
        raise ValueError("This task's position in its line was not recorded.")
    wanted = DONE_CHAR if completed else OPEN_CHAR
    line = line[: task.status_index] + wanted + line[task.status_index + 1 :]

    # 2. The done date. Emoji vaults get "✅ 2026-09-10"; dataview vaults get
    #    "[completion:: 2026-09-10]", because a vault settles on one syntax and
    #    rewriting someone's line into the other is not ours to do.
    done = next((t for t in task.tokens if t.field == "done"), None)
    stamp = (done_on or dt.date.today()).isoformat()

    if completed:
        if done is not None:
            start = task.body_offset + done.start
            end = task.body_offset + done.end
            replacement = (
                f"[completion:: {stamp}]" if done.syntax == "dataview" else f"✅ {stamp}"
            )
            line = line[:start] + replacement + line[end:]
        else:
            # Appended at the end of the metadata, before any block reference.
            # That is where the Tasks plugin's own field order puts it, so the
            # line stays one the plugin can still read.
            at = task.body_end
            addition = (
                f" [completion:: {stamp}]" if task.syntax == "dataview" else f" ✅ {stamp}"
            )
            line = line[:at] + addition + line[at:]
    elif done is not None:
        start = task.body_offset + done.start
        end = task.body_offset + done.end
        line = line[:start] + line[end:]
        # A removal leaves the two spaces that surrounded it; collapse just
        # those, without touching indentation at the front of the line.
        head, sep, tail = line.partition("] ")
        if sep:
            line = head + sep + re.sub(r"[ \t]{2,}", " ", tail).rstrip()

    return line


def verify_only_completion_changed(before: str, after: str) -> str | None:
    """Confirm a patched line differs from the original only where allowed.

    Called after the file has been written and read back, so a mistake is
    caught against what is actually on disk rather than what was intended. It
    returns an explanation, or None when the change is safe.

    The test is deliberately crude and strict: strip the status character and
    any done token from both sides, and what is left must be identical. Anything
    else -- a lost block reference, a mangled wikilink, a dropped tag -- shows up
    as a difference here and stops the write being accepted.
    """
    def skeleton(text: str) -> str | None:
        parsed = parse_line(text, 0)
        if parsed is None:
            return None
        body = parsed.raw[parsed.body_offset : parsed.body_end]
        token = next((t for t in parsed.tokens if t.field == "done"), None)
        if token is not None:
            body = body[: token.start] + body[token.end :]
        head = parsed.raw[: parsed.status_index] + parsed.raw[parsed.status_index + 1 :]
        head = head[: parsed.body_offset - 1]
        tail = parsed.raw[parsed.body_end :]
        return re.sub(r"\s+", " ", head + body + tail).strip()

    left, right = skeleton(before), skeleton(after)
    if left is None or right is None:
        return "The line stopped being a readable task after the change."
    if left != right:
        return (
            "The line changed in more than its completion. "
            f"Was: {left!r}. Became: {right!r}."
        )
    return None


# --- The reference back to the note -------------------------------------------

#: Marks the trailer Task Hub appends to a task's notes. Everything from this
#: line onwards belongs to Task Hub and is stripped before the notes are
#: compared or merged, so it can never be mistaken for something the user typed,
#: and can never accumulate a second copy of itself.
SOURCE_MARKER = "— Obsidian"


def source_reference(vault_name: str, relative_path: str) -> str:
    """The "where did this come from" trailer for a task's notes.

    A task lifted out of a vault and dropped into Todoist arrives as a bare
    line, stripped of the note that gave it its meaning. This puts the note
    back: the path, so it is readable anywhere, and an ``obsidian://`` URI,
    which opens the note itself on desktop and on mobile.
    """
    uri = (
        "obsidian://open?vault=" + quote(vault_name, safe="")
        + "&file=" + quote(relative_path, safe="")
    )
    return f"{SOURCE_MARKER} · {relative_path}\n{uri}"


def strip_source_reference(notes: str | None) -> str | None:
    """Remove a trailer this module added, leaving the user's own text.

    Applied to anything read back from a service before it is compared with the
    vault, so that Task Hub's own annotation is never counted as a change the
    user made, and never gets a second copy appended to it.
    """
    if not notes:
        return notes
    index = notes.find(SOURCE_MARKER)
    if index == -1:
        return notes
    return notes[:index].rstrip() or None


def with_source_reference(notes: str | None, reference: str) -> str:
    """The user's notes with exactly one trailer on the end."""
    body = strip_source_reference(notes)
    return f"{body}\n\n{reference}" if body else reference


# --- Turning a parsed task into a canonical record ----------------------------


def _normalise(text: str) -> str:
    """Collapse the whitespace and unicode forms a title can arrive in."""
    return unicodedata.normalize("NFC", " ".join(text.split()))


def to_record(
    task: InlineTask,
    *,
    uid: str,
    vault_name: str,
    relative_path: str,
    global_filter: str = "",
) -> CanonicalRecord:
    """One parsed line as the shape the rest of Task Hub speaks.

    Obsidian's dates carry no time and no timezone, so only the date components
    are ever set. That is not a gap to be filled with a default: the capability
    system treats a field a service cannot express as *absent*, which is what
    stops a task's 2:30pm being wiped out everywhere the moment it is read back
    from a vault that could never have held it.
    """
    title = task.description
    # The global filter is Obsidian's plumbing, not part of the task's name.
    if global_filter:
        title = title.replace(global_filter, "")
    for tag in task.tags:
        title = title.replace(f"#{tag}", "")
    title = _normalise(title)

    done = _parse_date(task.value("done") or "")
    completed_at = (
        dt.datetime.combine(done, dt.time.min, tzinfo=dt.timezone.utc) if done else None
    )

    return CanonicalRecord(
        uid=uid,
        kind=CollectionKind.TASKS,
        title=title,
        notes=source_reference(vault_name, relative_path),
        status=task.status,
        completed_at=completed_at,
        due_date=_parse_date(task.value("due") or ""),
        start_date=(_parse_date(task.value("start") or "")
                    or _parse_date(task.value("scheduled") or "")),
        priority=task.priority,
        # Obsidian's recurrence is natural language ("every week when done"),
        # not an RFC 5545 rule. Translating it would be a guess in both
        # directions, so it is left out of the canonical record rather than
        # turned into an RRULE that says something subtly different.
        rrule=None,
        tags=[t for t in task.tags if not (global_filter and f"#{t}" == global_filter)],
        origin_service=ServiceKind.OBSIDIAN,
    )


# --- TaskNotes ----------------------------------------------------------------
#
# The other shape a task takes: one file per task, its fields in YAML
# frontmatter. Structurally much the safer of the two, because the fields are
# real data rather than tokens embedded in prose, and a writer can round-trip
# them without the line-patching care that inline tasks demand.
#
# Two things differ from inline tasks in ways that matter. TaskNotes dates may
# carry a time of day ("2026-09-15T09:30"), so a task read from one is not
# necessarily date-only. And its recurrence is a real RFC 5545 rule
# ("FREQ=WEEKLY;BYDAY=MO") rather than the Tasks plugin's English, so it maps
# straight onto an RRULE with nothing lost or guessed.

#: TaskNotes lets the user rename every frontmatter property, so these are only
#: the defaults. The real mapping is read from the plugin's own settings where
#: they exist -- guessing a renamed field means silently losing it.
TASKNOTES_DEFAULT_FIELDS: dict[str, str] = {
    "title": "title",
    "status": "status",
    "priority": "priority",
    "due": "due",
    "scheduled": "scheduled",
    "contexts": "contexts",
    "projects": "projects",
    "recurrence": "recurrence",
    "completed": "completedDate",
    "created": "dateCreated",
    "modified": "dateModified",
    "archived": "archived",
}

#: Status values TaskNotes ships with. Users define their own, each with a
#: stored value and a display label, so an unrecognised one is treated as open
#: rather than guessed at -- calling something "done" that is not loses work.
TASKNOTES_STATUS = {
    "done": ItemStatus.COMPLETED,
    "completed": ItemStatus.COMPLETED,
    "complete": ItemStatus.COMPLETED,
    "in-progress": ItemStatus.IN_PROCESS,
    "in progress": ItemStatus.IN_PROCESS,
    "cancelled": ItemStatus.CANCELLED,
    "canceled": ItemStatus.CANCELLED,
    "open": ItemStatus.NEEDS_ACTION,
    "todo": ItemStatus.NEEDS_ACTION,
    "none": ItemStatus.NEEDS_ACTION,
}

#: Priority words, onto the same iCalendar 1-9 scale the inline format uses.
TASKNOTES_PRIORITY = {
    "highest": 1, "urgent": 1, "high": 3, "medium": 4,
    "normal": 0, "none": 0, "low": 6, "lowest": 9,
}

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.S)


@dataclass
class TaskNotesConfig:
    """How one vault's TaskNotes install is set up.

    Read from the plugin's own settings rather than assumed, because every
    property name is renameable and a task tag may or may not be in use. A
    vault with no TaskNotes installed simply uses the defaults, which costs
    nothing: files that are not tasks fail the test below anyway.
    """

    fields: dict[str, str] = field(default_factory=lambda: dict(TASKNOTES_DEFAULT_FIELDS))
    #: A tag that marks a file as a task, if the vault uses one.
    task_tag: str = ""

    def key(self, name: str) -> str:
        return self.fields.get(name, TASKNOTES_DEFAULT_FIELDS.get(name, name))


def load_tasknotes_config(settings: dict | None) -> TaskNotesConfig:
    """Read the plugin's settings blob, tolerating every version of its shape.

    The settings file belongs to somebody else's plugin and its layout is not a
    contract, so anything unrecognised falls back to the default rather than
    raising. Being wrong about a field name loses that field silently; refusing
    to start because a key moved would lose the whole vault.
    """
    config = TaskNotesConfig()
    if not isinstance(settings, dict):
        return config

    for candidate in ("fieldMapping", "field_mapping", "properties", "propertyNames"):
        mapping = settings.get(candidate)
        if isinstance(mapping, dict):
            for name, value in mapping.items():
                if isinstance(value, str) and value:
                    config.fields[name] = value
            break

    for candidate in ("taskTag", "task_tag", "taskIdentificationTag"):
        tag = settings.get(candidate)
        if isinstance(tag, str) and tag.strip():
            config.task_tag = tag.strip().lstrip("#")
            break

    return config


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a note into its YAML frontmatter and its body.

    Uses ``safe_load``: this is a file the user (or anything that ever wrote to
    their vault) controls, and the full loader can construct arbitrary Python
    objects from it. Malformed YAML yields no frontmatter rather than an error,
    because one bad note must not stop the other nine hundred syncing.
    """
    import yaml

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match[1])
    except Exception:       # noqa: BLE001 -- any parse failure, not just YAMLError
        return {}, text
    return (data if isinstance(data, dict) else {}), text[match.end():]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def is_tasknote(front: dict, config: TaskNotesConfig | None = None) -> bool:
    """Whether a note is a TaskNotes task rather than an ordinary note.

    The same principle as the inline rule: a note has to declare itself. Where
    the vault marks tasks with a tag, that tag decides. Otherwise the note needs
    a status *and* something that makes it a task -- a date, a priority, a
    recurrence. An ordinary note has none of those, so a whole vault of writing
    does not arrive in Todoist.
    """
    if not front:
        return False
    config = config or TaskNotesConfig()

    if config.task_tag:
        tags = {t.lstrip("#").lower() for t in _as_list(front.get("tags"))}
        return config.task_tag.lower() in tags

    if config.key("status") not in front:
        return False
    return any(
        config.key(name) in front
        for name in ("due", "scheduled", "priority", "recurrence", "completed")
    )


def _split_datetime(raw) -> tuple[dt.date | None, dt.time | None]:
    """A TaskNotes date, which may or may not carry a time of day.

    Unlike the inline format, these can be "2026-09-15T09:30". Returning the
    time separately rather than defaulting it to midnight is what keeps the
    distinction between "due that day" and "due at half past nine" -- and a
    midnight invented here would be pushed into every other service as a real
    appointment.
    """
    if isinstance(raw, dt.datetime):
        return raw.date(), raw.time()
    if isinstance(raw, dt.date):
        return raw, None
    text = str(raw or "").strip()
    if not text:
        return None, None
    date_part, _, time_part = text.partition("T")
    day = _parse_date(date_part)
    if day is None:
        return None, None
    if not time_part:
        return day, None
    try:
        clock = dt.time.fromisoformat(time_part.rstrip("Z")[:8])
    except ValueError:
        return day, None
    return day, clock


def tasknote_to_record(
    front: dict,
    *,
    uid: str,
    vault_name: str,
    relative_path: str,
    config: TaskNotesConfig | None = None,
) -> CanonicalRecord:
    """One TaskNotes file as the shape the rest of Task Hub speaks."""
    config = config or TaskNotesConfig()

    title = str(front.get(config.key("title")) or "").strip()
    if not title:
        # A TaskNotes file with no title is named by its file, which is what
        # Obsidian shows in every list anyway.
        title = relative_path.rsplit("/", 1)[-1].removesuffix(".md")

    status_raw = str(front.get(config.key("status")) or "").strip().lower()
    status = TASKNOTES_STATUS.get(status_raw, ItemStatus.NEEDS_ACTION)

    priority_raw = str(front.get(config.key("priority")) or "").strip().lower()
    priority = TASKNOTES_PRIORITY.get(priority_raw, 0)

    due_date, due_time = _split_datetime(front.get(config.key("due")))
    start_date, start_time = _split_datetime(front.get(config.key("scheduled")))
    done_date, _ = _split_datetime(front.get(config.key("completed")))

    tags = [t.lstrip("#") for t in _as_list(front.get("tags"))]
    if config.task_tag:
        tags = [t for t in tags if t.lower() != config.task_tag.lower()]
    # Contexts and projects are how TaskNotes says "where" and "what for", which
    # is what a tag means everywhere else Task Hub syncs to.
    tags += [
        str(v).strip("[]")
        for v in _as_list(front.get(config.key("contexts")))
        + _as_list(front.get(config.key("projects")))
    ]

    recurrence = str(front.get(config.key("recurrence")) or "").strip()

    return CanonicalRecord(
        uid=uid,
        kind=CollectionKind.TASKS,
        title=title,
        notes=source_reference(vault_name, relative_path),
        status=status,
        completed_at=(
            dt.datetime.combine(done_date, dt.time.min, tzinfo=dt.timezone.utc)
            if done_date else None
        ),
        due_date=due_date,
        due_time=due_time,
        start_date=start_date,
        start_time=start_time,
        priority=priority,
        # A real RFC 5545 rule, unlike the inline format's English, so it is
        # carried straight through rather than translated.
        rrule=recurrence or None,
        tags=sorted({t for t in tags if t}),
        origin_service=ServiceKind.OBSIDIAN,
    )
