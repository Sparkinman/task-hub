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
import secrets
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
_DATEISH_RE = re.compile(r"^\s*\d{1,5}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{1,5}\s*$")


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
            # The trailing time is matched only so that it can be *consumed*.
            # The Tasks plugin has no way to show a time on a line, so someone
            # writing "📅 2026-09-19T14:35" has made a mistake -- but leaving the
            # "T14:35" behind puts it in the task's title, where it travels out
            # to every other service as part of the name.
            found = re.match(
                r"\s*(\d{4}-\d{2}-\d{2})(?:T\d{1,2}:\d{2}(?::\d{2})?)?", raw_value)
            if found:
                value, end = found[1], value_at + found.end()
            else:
                # Not a readable date, which is usually a typo. Take only the
                # next word rather than the rest of the line: a mistyped year
                # ("📅 20206-09-18 Do the thing") would otherwise swallow the
                # description whole, and the task travels out to every other
                # service with a blank title and no deadline -- two losses from
                # one slip, and nothing on the page to say either happened.
                word = re.match(r"\s*(\S+)", raw_value)
                value, end = (word[1], value_at + word.end()) if word else ("", value_at)

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

#: Prefix on a block reference that Task Hub wrote itself.
#:
#: It is what makes a task Task Hub created identifiable ever afterwards.
#: :func:`stable_id` already prefers a block reference over a hash of the text,
#: so a task written into a vault keeps its identity when the user rewrites its
#: wording or moves the note it sits in -- which a text hash would not survive.
#:
#: Chosen to be visibly ours rather than random: somebody reading their own
#: notes should be able to tell which anchors they put there and which arrived.
TASKHUB_BLOCK_PREFIX = "th-"


def is_taskhub_block(block_id: str | None) -> bool:
    """Whether a block reference is one Task Hub wrote."""
    return bool(block_id and block_id.startswith(TASKHUB_BLOCK_PREFIX))


def new_block_id() -> str:
    """A fresh block reference for a task about to be written into a vault."""
    return f"{TASKHUB_BLOCK_PREFIX}{secrets.token_hex(4)}"


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
    if is_taskhub_block(task.block_id):
        # A line Task Hub wrote itself, which is a task by construction. This
        # clause is not a convenience: failing to recognise it would not merely
        # lose the task, it would make the next pass find it missing from the
        # vault and write it in a second time, once per pass, for ever.
        return True
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


# --- Writing a new task into a vault ------------------------------------------


#: iCalendar priority (1 most important, 9 least, 0 unset) to the Tasks
#: plugin's emoji. Five is deliberately absent: in iCalendar it means "normal",
#: and in the Tasks plugin normal is the *absence* of an emoji rather than one
#: of its own -- so writing 🔼 for a 5 would quietly promote every ordinary task
#: to medium on its way into somebody's vault.
def _priority_emoji(priority: int) -> str:
    if priority <= 0:
        return ""
    if priority <= 2:
        return "🔺"
    if priority == 3:
        return "⏫"
    if priority == 4:
        return "🔼"
    if priority == 5:
        return ""
    if priority <= 7:
        return "🔽"
    return "⏬"


def record_to_line(
    record: CanonicalRecord,
    *,
    block_id: str,
    global_filter: str = "",
) -> str:
    """One canonical record as a Tasks-plugin checklist line.

    The rough inverse of :func:`parse_line`, and deliberately much narrower than
    it. This builds a line from nothing, which is safe precisely because there
    is no existing line to damage -- the rule that a line must be patched rather
    than regenerated protects text somebody else wrote, and here nobody has
    written any yet.

    What it will not do is invent. A time of day is dropped rather than
    approximated, because the emoji syntax has no way to say "2:30pm" and a date
    that silently gained a time would travel back out to every other service as
    an edit nobody made. Notes are dropped for the same reason: there is nowhere
    on the line to put them.

    The trailing block reference is what makes the task identifiable ever after.
    :func:`stable_id` prefers it over the hash of the text, so the task survives
    being reworded, reordered or moved to another note -- none of which a hash
    of "path plus description" would survive.
    """
    done = record.status == ItemStatus.COMPLETED
    pieces: list[str] = [f"- [{DONE_CHAR if done else OPEN_CHAR}]"]

    # The vault's own global filter, where it has one, so that Obsidian's idea
    # of the task list and Task Hub's stay the same list. Without this the task
    # would sit in the vault invisible to every one of the user's own queries.
    if global_filter:
        pieces.append(global_filter)

    pieces.append((record.title or "").strip() or "Untitled task")

    for tag in record.tags or []:
        cleaned = str(tag).strip().lstrip("#")
        if cleaned:
            pieces.append(f"#{cleaned}")

    emoji = _priority_emoji(record.priority or 0)
    if emoji:
        pieces.append(emoji)

    # Order matters: the Tasks plugin reads a line backwards from the end and
    # stops at the first thing it does not recognise, so the metadata goes last
    # and in the plugin's own order.
    if record.rrule:
        pieces.append(f"🔁 {record.rrule}")
    if record.start_date:
        pieces.append(f"🛫 {record.start_date.isoformat()}")
    if record.due_date:
        pieces.append(f"📅 {record.due_date.isoformat()}")
    if done:
        stamp = (record.completed_at.date() if record.completed_at
                 else dt.date.today())
        pieces.append(f"✅ {stamp.isoformat()}")

    pieces.append(f"^{block_id}")
    return " ".join(pieces)


# --- Writing a new TaskNotes file ---------------------------------------------


#: Characters no common filesystem will take in a name, plus the ones Obsidian
#: reads as link syntax. Replaced rather than stripped, so two tasks whose names
#: differ only there do not collide into one file.
_UNSAFE_IN_FILENAME = re.compile(r'[\\/:*?"<>|\[\]#^]+')


def tasknote_filename(title: str) -> str:
    """A file name for a task, from its title.

    TaskNotes names a task's file after the task, which is why a note with no
    ``title`` property still has a name. Keeping that convention means a task
    written by Task Hub is indistinguishable from one the user made.
    """
    cleaned = _UNSAFE_IN_FILENAME.sub("-", (title or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    # Long titles are real -- a forwarded email subject line arrives as one --
    # and most filesystems stop at 255 bytes for the whole name.
    return (cleaned[:120].rstrip() or "Untitled task")


def _yaml_scalar(value: str) -> str:
    """One frontmatter value, quoted only when it has to be.

    Hand-written rather than dumped with PyYAML because the surrounding file is
    read by other people's plugins: block scalars, anchors and flow style are
    all valid YAML and all unlike anything TaskNotes itself writes.
    """
    text = str(value)
    if text == "":
        return '""'
    needs_quotes = (
        text[0] in "-?:,[]{}#&*!|>'\"%@`"
        or text[-1] in " :"
        or ": " in text
        or text.strip() != text
        or text.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~")
    )
    if not needs_quotes:
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def record_to_tasknote(
    record: CanonicalRecord,
    *,
    config: TaskNotesConfig | None = None,
    parent_note: str = "",
    now: dt.datetime | None = None,
) -> str:
    """One canonical record as a whole TaskNotes file.

    The safest write in this module by a distance: a new file touches nothing
    anybody else wrote, so none of the line-patching care that inline tasks
    demand applies. It is also the only format here that can hold a time of day
    and a description, both of which an inline task silently drops.

    Every value is written in the vault's own vocabulary -- its word for "open",
    its name for the due property -- so the result is indistinguishable from a
    task the user created, and their own saved views find it.

    ``scheduled`` is deliberately never written. Task Hub does not read it, and
    writing a field it refuses to read would make a task change every time it
    was looked at.
    """
    config = config or TaskNotesConfig()
    stamp = (now or dt.datetime.now().astimezone()).replace(microsecond=0)

    lines: list[str] = ["---"]

    title = (record.title or "").strip() or "Untitled task"
    lines.append(f"{config.key('title')}: {_yaml_scalar(title)}")

    lines.append(f"{config.key('status')}: "
                 f"{_yaml_scalar(config.value_for_status(record.status))}")

    priority = config.value_for_priority(record.priority or 0)
    if priority:
        lines.append(f"{config.key('priority')}: {_yaml_scalar(priority)}")

    if record.due_date:
        if record.due_time:
            value = f"{record.due_date.isoformat()}T{record.due_time.strftime('%H:%M')}"
            # Only when the record names a zone. A floating time written with a
            # Z would be moved by every reader's own offset -- the same fault in
            # the opposite direction from dropping the offset when reading.
            if (record.due_tz or "").upper() == "UTC":
                value += "Z"
        else:
            value = record.due_date.isoformat()
        lines.append(f"{config.key('due')}: {_yaml_scalar(value)}")

    if record.status == ItemStatus.COMPLETED:
        done = (record.completed_at.date() if record.completed_at else stamp.date())
        lines.append(f"{config.key('completed')}: {_yaml_scalar(done.isoformat())}")

    if record.rrule:
        lines.append(f"{config.key('recurrence')}: {_yaml_scalar(record.rrule)}")

    # Containment, in the only form TaskNotes has for it.
    if parent_note:
        lines.append(f"{config.key('projects')}:")
        lines.append(f'  - "[[{parent_note}]]"')

    lines.append(f"{config.key('created')}: {stamp.isoformat()}")
    lines.append(f"{config.key('modified')}: {stamp.isoformat()}")

    tags = []
    if config.task_tag:
        tags.append(config.task_tag)
    for tag in record.tags or []:
        cleaned = str(tag).strip().lstrip("#")
        if cleaned and cleaned.lower() not in {t.lower() for t in tags}:
            tags.append(cleaned)
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {_yaml_scalar(tag)}" for tag in tags)

    lines.append("---")

    body = strip_source_reference(record.notes) or ""
    if body.strip():
        lines.append("")
        lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


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

#: A wikilink, which is how TaskNotes writes a reference to another note --
#: ``projects: ["[[A bUnch of blocked tasks]]"]``. The display half of
#: ``[[Note|shown as this]]`` is dropped: the target is what identifies it.
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def wikilink_target(value) -> str:
    """The note a value points at, or "" if it is not a link."""
    match = _WIKILINK_RE.search(str(value or ""))
    return match[1].strip() if match else ""


def tasknote_project_links(front: dict, config: "TaskNotesConfig | None" = None) -> list[str]:
    """The notes this task names as its projects, in order.

    TaskNotes has no "parent" field. A subtask points at the note it belongs to
    through ``projects``, so that is where containment is expressed -- but only
    when the value is a link. A project written as a plain word is a label, not
    a relationship, and treating it as one would invent a parent that does not
    exist.
    """
    config = config or TaskNotesConfig()
    targets = []
    for value in _as_list(front.get(config.key("projects"))):
        target = wikilink_target(value)
        if target and target not in targets:
            targets.append(target)
    return targets


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
    #: How the vault says "this note is a task": by tag, or by a frontmatter
    #: property having a particular value. Both are offered by the plugin, and
    #: assuming the tag in a vault that uses the property reads every task as an
    #: ordinary note -- the whole task list simply does not arrive.
    identify_by: str = "tag"
    property_name: str = ""
    property_value: str = ""
    #: The vault's own statuses, by stored value. The plugin records whether
    #: each one counts as finished, so this is read rather than inferred from
    #: the word: a vault calling its finished state "archived" or "shipped" is
    #: perfectly legal, and guessing from the name would leave those tasks open
    #: for ever everywhere else.
    statuses: dict[str, ItemStatus] = field(default_factory=dict)
    #: The vault's own priorities, by stored value, already on iCalendar's 1-9.
    priorities: dict[str, int] = field(default_factory=dict)
    #: Where the plugin puts new task notes, and how it names them. Used when
    #: Task Hub creates one, so that a task arriving from Google lands where the
    #: user's own tasks land rather than somewhere of our choosing.
    tasks_folder: str = ""
    filename_format: str = "title"
    filename_template: str = "{{title}}"
    default_status: str = ""
    default_priority: str = ""

    def key(self, name: str) -> str:
        return self.fields.get(name, TASKNOTES_DEFAULT_FIELDS.get(name, name))

    def status_for(self, raw: str) -> ItemStatus:
        """The canonical status for one of this vault's status values."""
        value = (raw or "").strip().lower()
        if value in self.statuses:
            return self.statuses[value]
        return TASKNOTES_STATUS.get(value, ItemStatus.NEEDS_ACTION)

    def priority_for(self, raw: str) -> int:
        value = (raw or "").strip().lower()
        if value in self.priorities:
            return self.priorities[value]
        return TASKNOTES_PRIORITY.get(value, 0)

    def value_for_status(self, status: ItemStatus) -> str:
        """This vault's own word for a status, for writing a task note.

        Falls back to the plugin's shipped vocabulary, and then to the vault's
        default, so a write never invents a status value the user's own filters
        have never heard of.
        """
        # The vault's own default first. Several values can mean "not done" --
        # this vault has both "none" and "open" -- and the default is the one
        # the plugin itself puts on a task it creates, so it is the one that
        # will look native in the user's own views.
        if self.default_status:
            default = self.default_status.strip().lower()
            if self.statuses.get(default) == status:
                return default
        for value, mapped in self.statuses.items():
            if mapped == status and value != "none":
                return value
        for value, mapped in self.statuses.items():
            if mapped == status:
                return value
        shipped = {
            ItemStatus.COMPLETED: "done",
            ItemStatus.IN_PROCESS: "in-progress",
            ItemStatus.CANCELLED: "cancelled",
        }
        return shipped.get(status, self.default_status or "open")

    def value_for_priority(self, priority: int) -> str:
        """This vault's own word for an iCalendar priority, or "" for unset."""
        if not priority:
            return ""
        table = self.priorities or TASKNOTES_PRIORITY
        best, distance = "", None
        for value, mapped in table.items():
            if not mapped:
                continue
            gap = abs(mapped - priority)
            if distance is None or gap < distance:
                best, distance = value, gap
        return best


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

    method = settings.get("taskIdentificationMethod")
    if isinstance(method, str) and method.strip():
        config.identify_by = method.strip().lower()
    for name, attribute in (("taskPropertyName", "property_name"),
                            ("taskPropertyValue", "property_value")):
        value = settings.get(name)
        if isinstance(value, str) and value.strip():
            setattr(config, attribute, value.strip())

    # Statuses. ``isCompleted`` is the plugin's own answer to "is this task
    # finished", so it is believed in preference to the word itself.
    for entry in settings.get("customStatuses") or []:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or "").strip().lower()
        if not value:
            continue
        if entry.get("isCompleted"):
            config.statuses[value] = ItemStatus.COMPLETED
        else:
            config.statuses[value] = TASKNOTES_STATUS.get(value, ItemStatus.NEEDS_ACTION)
            if config.statuses[value] == ItemStatus.COMPLETED:
                # The word says done, the plugin says it is not. The plugin wins:
                # it is what the user's own views act on.
                config.statuses[value] = ItemStatus.NEEDS_ACTION

    # Priorities. The plugin stores a weight, higher meaning more important,
    # which is the opposite direction from iCalendar's 1-9. A known word keeps
    # its usual meaning; anything the vault invented is placed by its weight.
    entries = [e for e in (settings.get("customPriorities") or []) if isinstance(e, dict)]
    ranked = sorted(
        ((str(e.get("value") or "").strip().lower(), e.get("weight"))
         for e in entries if str(e.get("value") or "").strip()),
        key=lambda pair: pair[1] if isinstance(pair[1], (int, float)) else 0,
    )
    real = [(value, weight) for value, weight in ranked
            if isinstance(weight, (int, float)) and weight > 0]
    for position, (value, _weight) in enumerate(real):
        if value in TASKNOTES_PRIORITY:
            config.priorities[value] = TASKNOTES_PRIORITY[value]
            continue
        # Least important first, spread across 9 down to 1.
        span = max(len(real) - 1, 1)
        config.priorities[value] = round(9 - (position * 8 / span))
    for value, weight in ranked:
        if not isinstance(weight, (int, float)) or weight <= 0:
            config.priorities.setdefault(value, 0)

    for name, attribute in (("tasksFolder", "tasks_folder"),
                            ("taskFilenameFormat", "filename_format"),
                            ("customFilenameTemplate", "filename_template"),
                            ("defaultTaskStatus", "default_status"),
                            ("defaultTaskPriority", "default_priority")):
        value = settings.get(name)
        if isinstance(value, str) and value.strip():
            setattr(config, attribute, value.strip())

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

    # A vault can be set to mark tasks with a frontmatter property instead of a
    # tag. Reading the tag in that vault finds nothing at all, so the setting is
    # honoured rather than assumed.
    if config.identify_by == "property" and config.property_name:
        held = front.get(config.property_name)
        wanted = config.property_value
        if not wanted:
            return held not in (None, "", [], {})
        return any(str(v).strip().lower() == wanted.strip().lower()
                   for v in _as_list(held) or [held])

    if config.task_tag:
        tags = {t.lstrip("#").lower() for t in _as_list(front.get("tags"))}
        return config.task_tag.lower() in tags

    if config.key("status") not in front:
        return False
    return any(
        config.key(name) in front
        for name in ("due", "scheduled", "priority", "recurrence", "completed")
    )


def _split_datetime(raw) -> tuple[dt.date | None, dt.time | None, str | None]:
    """A TaskNotes date: the day, the time of day if it has one, and its zone.

    Unlike the inline format, these can be "2026-09-15T09:30". Returning the
    time separately rather than defaulting it to midnight is what keeps the
    distinction between "due that day" and "due at half past nine" -- and a
    midnight invented here would be pushed into every other service as a real
    appointment.

    **The offset is the part that must not be dropped.** A value like
    "2026-09-19T14:35:00-06:00" names an instant; the same digits with no offset
    name a wall clock, and the two are different times. Keeping the digits and
    discarding the zone is precisely the fault that moved 103 real Google
    Calendar events by an hour, so an offset is converted to UTC and said so,
    while a value with no offset stays floating and is reported as such.
    """
    if isinstance(raw, dt.datetime):
        if raw.tzinfo is not None:
            moment = raw.astimezone(dt.timezone.utc)
            return moment.date(), moment.time(), "UTC"
        return raw.date(), raw.time(), None
    if isinstance(raw, dt.date):
        return raw, None, None
    text = str(raw or "").strip()
    if not text:
        return None, None, None
    date_part, _, time_part = text.partition("T")
    day = _parse_date(date_part)
    if day is None:
        return None, None, None
    if not time_part:
        return day, None, None

    normalised = time_part.strip()
    if normalised.endswith(("Z", "z")):
        normalised = normalised[:-1] + "+00:00"
    try:
        parsed = dt.time.fromisoformat(normalised)
    except ValueError:
        # Not a time we can read. The day is still good, and inventing a clock
        # face for it would be worse than admitting the time is unknown.
        try:
            parsed = dt.time.fromisoformat(normalised[:8])
        except ValueError:
            return day, None, None
        return day, parsed, None

    if parsed.tzinfo is None:
        return day, parsed, None

    moment = dt.datetime.combine(day, parsed).astimezone(dt.timezone.utc)
    return moment.date(), moment.time().replace(tzinfo=None), "UTC"


def tasknote_to_record(
    front: dict,
    *,
    uid: str,
    vault_name: str,
    relative_path: str,
    config: TaskNotesConfig | None = None,
    body: str = "",
) -> CanonicalRecord:
    """One TaskNotes file as the shape the rest of Task Hub speaks.

    The body is the task's description, and it is the second thing this format
    can carry that an inline task cannot. Leaving it out meant a description
    written in Obsidian never reached Google, and one written by Task Hub did
    not survive being read back -- which reads as the text having been deleted.
    """
    config = config or TaskNotesConfig()

    title = str(front.get(config.key("title")) or "").strip()
    if not title:
        # A TaskNotes file with no title is named by its file, which is what
        # Obsidian shows in every list anyway.
        title = relative_path.rsplit("/", 1)[-1].removesuffix(".md")

    # Both go through the vault's own vocabulary first, falling back to the
    # plugin's shipped words only for a value it does not define.
    status = config.status_for(str(front.get(config.key("status")) or ""))
    priority = config.priority_for(str(front.get(config.key("priority")) or ""))

    due_date, due_time, due_tz = _split_datetime(front.get(config.key("due")))
    done_date, done_time, done_tz = _split_datetime(front.get(config.key("completed")))

    # ``scheduled`` is deliberately not read as a start date.
    #
    # TaskNotes fills it in on every task it creates -- its defaultScheduledDate
    # ships as "today" -- so it records when the task was made rather than when
    # it may be begun. Carried outward as a start date it becomes a range nobody
    # set, and in Todoist a task with a start date is shown at its start: the
    # whole list silently reorders itself around a value the user never chose.
    # Same fault as TickTick's mirrored start date, and the same answer.
    start_date, start_time = None, None

    tags = [t.lstrip("#") for t in _as_list(front.get("tags"))]
    if config.task_tag:
        tags = [t for t in tags if t.lower() != config.task_tag.lower()]
    # Contexts and projects are how TaskNotes says "where" and "what for", which
    # is what a tag means everywhere else Task Hub syncs to.
    # Contexts and projects are how TaskNotes says "where" and "what for", which
    # is what a tag means everywhere else Task Hub syncs to -- except where a
    # project is a *link*, which is containment rather than a label and is
    # carried as the task's parent instead. Keeping it in both places would put
    # the parent's name on the child as a tag in every other service.
    tags += [
        str(v).strip("[]")
        for v in _as_list(front.get(config.key("contexts")))
    ]
    tags += [
        str(v).strip("[]")
        for v in _as_list(front.get(config.key("projects")))
        if not wikilink_target(v)
    ]

    recurrence = str(front.get(config.key("recurrence")) or "").strip()

    return CanonicalRecord(
        uid=uid,
        kind=CollectionKind.TASKS,
        title=title,
        notes=with_source_reference(
            (body or "").strip() or None, source_reference(vault_name, relative_path)),
        status=status,
        completed_at=(
            dt.datetime.combine(
                done_date, done_time or dt.time.min, tzinfo=dt.timezone.utc)
            if done_date else None
        ),
        due_date=due_date,
        due_time=due_time,
        # Only ever "UTC", and only when the file named an offset. A due time
        # with no zone is a wall clock and must stay one: saying UTC of a
        # floating time would move it by the reader's own offset.
        due_tz=due_tz,
        start_date=start_date,
        start_time=start_time,
        priority=priority,
        # A real RFC 5545 rule, unlike the inline format's English, so it is
        # carried straight through rather than translated.
        rrule=recurrence or None,
        tags=sorted({t for t in tags if t}),
        origin_service=ServiceKind.OBSIDIAN,
    )
