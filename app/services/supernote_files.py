"""Supernote Cloud's file store: browsing folders and converting notes to PDF.

Separate from :mod:`app.connectors.supernote`, which syncs to-dos, because this
talks to a different host with a different gate. The to-do API lives on
``viewer.supernote.com`` and needs no handshake; the file API lives on
``cloud.supernote.com`` and refuses everything until a CSRF token has been
fetched. The session token itself is shared, so one sign-in covers both.

Nothing here writes to Supernote. It lists folders, reads notes, and asks
Supernote's own converter to render them -- which matters, because ``.note`` is
an undocumented binary format and the community converters lag firmware
changes. Ratta's converter always understands the current format because it is
the one that wrote it.

Two things learned by probing that the code depends on:

``pageNoList: []`` means "every page". Supplying an explicit range fails with
"The QT program failed to parse the file!" whenever the range does not exactly
match the note, so asking for all pages is both simpler and the only reliable
form -- and it makes one request per note rather than one to count pages and
another to convert them. On somebody else's server, at their expense, that
halving is the difference between polite and rude.

Every file row carries an ``md5``. Change detection uses it rather than
timestamps, so a note that was opened but not altered is never reconverted.
"""

from __future__ import annotations

import dataclasses
import logging

import httpx

from app.connectors.base import ConnectorAuthError, ConnectorError

logger = logging.getLogger(__name__)

#: The file store. Not viewer.supernote.com, which serves the to-do API and has
#: no file routes at all.
BASE_URL = "https://cloud.supernote.com/api"

LIST = "/file/list/query"
NOTE_TO_PDF = "/file/note/to/pdf"

#: Supernote's own top-level folders. Offered first in the picker because they
#: are what everybody actually has.
WELL_KNOWN_ROOTS = ("Note", "Document", "Screenshot", "Inbox", "Export", "MyStyle")


@dataclasses.dataclass(frozen=True)
class Entry:
    """One row from a folder listing: a folder, or a file."""

    id: str
    name: str
    is_folder: bool
    parent_id: str
    size: int = 0
    md5: str = ""
    updated_at: int = 0  # epoch milliseconds, as the API gives it

    @property
    def is_note(self) -> bool:
        return not self.is_folder and self.name.lower().endswith(".note")

    @property
    def stem(self) -> str:
        """The name without its .note suffix, for naming the PDF."""
        return self.name[:-5] if self.name.lower().endswith(".note") else self.name


class SupernoteFiles:
    """A read-only view of one account's Supernote Cloud files."""

    def __init__(self, token: str, client: httpx.Client | None = None):
        if not token:
            raise ConnectorAuthError("This Supernote account has no saved session.")
        self._token = token
        self._client = client or httpx.Client(timeout=60)
        self._owns_client = client is None
        self._xsrf: str | None = None

    # -- Plumbing -------------------------------------------------------------

    def _ensure_csrf(self) -> None:
        """Fetch the token every other call on this host is refused without."""
        if self._xsrf:
            return
        try:
            response = self._client.get(f"{BASE_URL}/csrf", timeout=30)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Could not reach Supernote Cloud: {exc}") from exc
        for name, value in response.headers.items():
            if name.lower() == "x-xsrf-token" and value:
                self._xsrf = value
        if not self._xsrf:
            raise ConnectorError(
                "Supernote Cloud would not issue a CSRF token, so its file "
                "store cannot be reached."
            )

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        self._ensure_csrf()
        headers = {
            "Content-Type": "application/json",
            "x-access-token": self._token,
            "X-XSRF-TOKEN": self._xsrf or "",
        }
        try:
            response = self._client.post(
                BASE_URL + path, json=payload, headers=headers, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            # Distinguished from a general failure because it is not a fault and
            # not permanent: converting a large notebook simply takes a while,
            # and the next pass will try again.
            raise ConnectorError(
                "Supernote took too long to answer. Large notebooks can time "
                "out; this one will be tried again on the next backup."
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Could not reach Supernote Cloud: {exc}") from exc

        if response.status_code in (502, 503, 504):
            # A gateway error is their converter giving up, most often on a big
            # planner. Saying the API "may have changed" here would send someone
            # looking for a fault that is not there.
            raise ConnectorError(
                f"Supernote's server gave up while working on this "
                f"({response.status_code}). Large notebooks do this; it will be "
                "tried again on the next backup."
            )

        if response.status_code in (401, 403):
            raise ConnectorAuthError(
                "Supernote Cloud rejected this session. Sign in again on the "
                "Supernote page to renew it."
            )
        try:
            body = response.json()
        except ValueError:
            raise ConnectorError(
                f"Supernote Cloud answered {response.status_code} with something "
                "that was not JSON. The unofficial API it uses may have changed."
            ) from None
        if not isinstance(body, dict):
            raise ConnectorError("Supernote Cloud returned an unexpected response.")
        if body.get("success") is False:
            raise ConnectorError(body.get("errorMsg") or f"Supernote refused {path}.")
        return body

    # -- Browsing -------------------------------------------------------------

    def list_folder(self, directory_id: str = "0") -> list[Entry]:
        """One folder's contents. ``"0"`` is the root.

        The sort arguments are not optional: without them the server answers
        "Sorting condition cannot be empty", which is easy to mistake for an
        authentication problem because it arrives with a 200.
        """
        entries: list[Entry] = []
        page = 1
        while True:
            body = self._post(LIST, {
                "directoryId": str(directory_id),
                "pageNo": page,
                "pageSize": 100,
                "order": "time",
                "sequence": "desc",
            })
            rows = body.get("userFileVOList") or []
            for row in rows:
                entries.append(Entry(
                    id=str(row.get("id") or ""),
                    name=(row.get("fileName") or "").strip(),
                    is_folder=str(row.get("isFolder") or "").upper() == "Y",
                    parent_id=str(row.get("directoryId") or ""),
                    size=int(row.get("size") or 0),
                    md5=str(row.get("md5") or ""),
                    updated_at=int(row.get("updateTime") or 0),
                ))
            total_pages = int(body.get("pages") or 1)
            if page >= total_pages or not rows:
                break
            page += 1
        return [e for e in entries if e.id]

    def folders_in(self, directory_id: str = "0") -> list[Entry]:
        return [e for e in self.list_folder(directory_id) if e.is_folder]

    def notes_under(self, directory_id: str, max_depth: int = 6) -> list[tuple[Entry, str]]:
        """Every ``.note`` at or below a folder, each with its display path.

        Depth-limited rather than trusting the tree to be finite: this walks
        somebody else's server, and a cycle or a pathological hierarchy would
        otherwise turn a backup into an unbounded crawl.
        """
        found: list[tuple[Entry, str]] = []
        stack: list[tuple[str, str, int]] = [(str(directory_id), "", 0)]
        seen: set[str] = set()

        while stack:
            folder_id, prefix, depth = stack.pop()
            if folder_id in seen:
                continue
            seen.add(folder_id)
            for entry in self.list_folder(folder_id):
                if entry.is_folder:
                    if depth < max_depth:
                        stack.append((entry.id, f"{prefix}{entry.name}/", depth + 1))
                elif entry.is_note:
                    found.append((entry, prefix))
        return found

    # -- Converting -----------------------------------------------------------

    def note_pdf(self, note_id: str) -> bytes:
        """Render one note to PDF using Supernote's own converter.

        ``pageNoList: []`` asks for the whole note. An explicit page range is
        rejected unless it matches the note exactly -- with "The QT program
        failed to parse the file!", which says nothing about pages -- so the
        empty list is both the simplest and the only dependable form.
        """
        # A generous timeout: rendering a year-long planner is slow, and giving
        # up early turns a notebook that would have converted into a permanent
        # gap in the backup.
        body = self._post(
            NOTE_TO_PDF, {"id": str(note_id), "type": 0, "pageNoList": []},
            timeout=300,
        )
        url = body.get("url")
        if not url:
            raise ConnectorError("Supernote converted the note but returned no address for it.")
        try:
            response = self._client.get(url, timeout=300)
        except httpx.TimeoutException as exc:
            raise ConnectorError(
                "The converted notebook took too long to download. It will be "
                "tried again on the next backup."
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Could not download the converted note: {exc}") from exc
        if response.status_code != 200:
            raise ConnectorError(
                f"Downloading the converted note failed with {response.status_code}."
            )
        content = response.content
        if not content.startswith(b"%PDF"):
            # A signed URL that has expired answers with an XML error document
            # rather than a failure status, which would otherwise be saved as a
            # PDF that no reader can open.
            raise ConnectorError(
                "Supernote returned something that was not a PDF. The conversion "
                "may have failed for this note."
            )
        return content

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
