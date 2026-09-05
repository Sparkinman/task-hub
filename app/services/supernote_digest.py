"""Supernote's Digest: the highlights pulled out of documents and notebooks.

Supernote calls these "digests" in its interface and "summaries" in its API,
and the two words mean the same thing. A digest is a passage dragged out of a
PDF or a notebook, kept with the file and page it came from, and filed into a
library.

Finding this was mostly a matter of one wrong assumption. The endpoints were
readable in the Partner app, and every one of them answered a bare "Server
Error" whatever was sent -- for hours -- because the paging arguments are
``page`` and ``size`` where the rest of Supernote's API uses ``pageNo`` and
``pageSize``. A missing ``page`` throws inside the handler rather than being
validated, so there is nothing in the response to suggest which of the dozen
plausible causes it is.

The verbs are the second half of it, and they follow the same pattern as the
to-do API: ``POST`` to read and to create, ``PUT`` to change, ``DELETE`` to
remove. Sending ``POST`` to the delete route produces the same unhelpful 500,
which is what made it look unreachable.

Every operation here has been exercised against a live account: created a
digest, read it back, edited it, deleted it, and confirmed the account was left
exactly as it was found.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import uuid

import httpx

from app.connectors.base import ConnectorAuthError, ConnectorError

logger = logging.getLogger(__name__)

#: Same host and session as the to-do connector.
BASE_URL = "https://viewer.supernote.com/api"

QUERY_DIGESTS = "file/query/summary"
QUERY_LIBRARIES = "file/query/summary/group"
QUERY_HASHES = "file/query/summary/hash"
ADD_DIGEST = "file/add/summary"
ADD_LIBRARY = "file/add/summary/group"
UPDATE_DIGEST = "file/update/summary"
DELETE_DIGEST = "file/delete/summary"
DELETE_LIBRARY = "file/delete/summary/group"
DOWNLOAD = "file/download/summary"

#: The page size to ask for. Generous because a digest is a paragraph at most
#: and an account holds tens of them, not thousands.
PAGE_SIZE = 200

#: Supernote's own numbering for where a digest came from. Only used to say so
#: on the page; nothing depends on it.
SOURCE_TYPES = {1: "Document", 2: "Notebook", 4: "Added elsewhere"}


@dataclasses.dataclass(frozen=True)
class Library:
    """A digest library -- what the tablet calls a category."""

    id: str
    name: str
    unique_id: str


@dataclasses.dataclass(frozen=True)
class Digest:
    """One highlighted passage, with where it came from."""

    id: str
    content: str
    library_uid: str
    source_path: str
    source_type: int
    comment: str
    handwriting: str
    handwriting_md5: str
    md5: str
    created_at: int
    modified_at: int
    metadata: dict

    @property
    def page_number(self) -> int | None:
        """The page of the source document, dug out of the metadata blob.

        Supernote stores it as a JSON string inside a JSON field, which is why
        this is parsed defensively rather than indexed into: a change of shape
        should cost the page number, not the digest.
        """
        try:
            raw = self.metadata.get("document_location_data")
            if not raw:
                return None
            spots = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(spots, list) and spots:
                page = spots[0].get("page")
                return int(page) if page is not None else None
        except (ValueError, TypeError, AttributeError, KeyError):
            return None
        return None

    @property
    def source_name(self) -> str:
        """Just the file, without its folders."""
        return (self.source_path or "").rsplit("/", 1)[-1]


class SupernoteDigests:
    """Read and write one account's digests."""

    def __init__(self, token: str, client: httpx.Client | None = None):
        if not token:
            raise ConnectorAuthError("This Supernote account has no saved session.")
        self._token = token
        self._client = client or httpx.Client(timeout=45)
        self._owns_client = client is None

    # -- Plumbing -------------------------------------------------------------

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json", "x-access-token": self._token}
        try:
            response = self._client.request(
                method, f"{BASE_URL}/{path}", json=payload or {}, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Could not reach Supernote Cloud: {exc}") from exc

        if response.status_code in (401, 403):
            raise ConnectorAuthError(
                "Supernote Cloud rejected this session. Sign in again on the "
                "Supernote page to renew it."
            )
        try:
            body = response.json()
        except ValueError:
            raise ConnectorError(
                f"Supernote answered {response.status_code} with something that "
                "was not JSON. The unofficial API it uses may have changed."
            ) from None
        if body.get("success") is False:
            message = body.get("errorMsg") or f"Supernote refused {path}."
            if message == "Server Error, please try again later":
                # The signature of a wrong request shape rather than a fault:
                # this handler throws on a missing argument instead of saying
                # which. Worth naming, because the next person to see it will
                # otherwise go looking for an outage.
                message = (
                    "Supernote refused the request without saying why, which "
                    "usually means this version is sending the wrong arguments "
                    "for its API rather than that anything is down."
                )
            raise ConnectorError(message)
        return body

    def _pages(self, path: str, key: str) -> list[dict]:
        """Every row from a paged endpoint.

        ``page`` and ``size``, not ``pageNo`` and ``pageSize``. The rest of
        Supernote's API uses the latter, this controller uses the former, and
        getting it wrong produces a bare "Server Error" rather than a complaint
        about a missing argument.
        """
        rows: list[dict] = []
        page = 1
        while True:
            body = self._call("POST", path, {"page": page, "size": PAGE_SIZE})
            batch = body.get(key) or []
            rows.extend(batch)
            if page >= int(body.get("totalPages") or 1) or not batch:
                break
            page += 1
        return rows

    # -- Reading --------------------------------------------------------------

    def libraries(self) -> list[Library]:
        found = []
        for row in self._pages(QUERY_LIBRARIES, "summaryDOList"):
            if str(row.get("isDeleted") or "").upper() == "Y":
                continue
            found.append(Library(
                id=str(row.get("id") or ""),
                name=(row.get("name") or "").strip() or "Untitled library",
                unique_id=str(row.get("uniqueIdentifier") or ""),
            ))
        return [lib for lib in found if lib.id]

    def digests(self) -> list[Digest]:
        found = []
        for row in self._pages(QUERY_DIGESTS, "summaryDOList"):
            if str(row.get("isDeleted") or "").upper() == "Y":
                continue
            if str(row.get("isSummaryGroup") or "").upper() == "Y":
                continue  # A library row, not a digest.

            metadata = {}
            raw = row.get("metadata")
            if raw:
                try:
                    metadata = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (ValueError, TypeError):
                    metadata = {}

            found.append(Digest(
                id=str(row.get("id") or ""),
                content=(row.get("content") or "").strip(),
                library_uid=str(row.get("parentUniqueIdentifier") or ""),
                source_path=(row.get("sourcePath") or "").strip(),
                source_type=int(row.get("sourceType") or 0),
                comment=(row.get("commentStr") or "").strip(),
                handwriting=(row.get("commentHandwriteName") or "").strip(),
                handwriting_md5=str(row.get("handwriteMD5") or ""),
                md5=str(row.get("md5Hash") or ""),
                created_at=int(row.get("creationTime") or 0),
                modified_at=int(row.get("lastModifiedTime") or 0),
                metadata=metadata,
            ))
        return [d for d in found if d.id]

    def handwriting_bytes(self, digest_id: str) -> bytes | None:
        """The ``.mark`` file holding a digest's handwritten note.

        Answers with a signed address rather than the file, so this is two
        requests: one to Supernote for the address, one to their storage for
        the bytes.
        """
        body = self._call("POST", DOWNLOAD, {"id": int(digest_id)})
        url = body.get("url")
        if not url:
            return None
        try:
            response = self._client.get(url, timeout=90)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Could not download the handwriting: {exc}") from exc
        if response.status_code != 200:
            return None
        content = response.content
        # Their own container format. Anything else means the address expired
        # and this is an error document, which must not be stored as a drawing.
        return content if b"SN_FILE" in content[:64] else None

    def raw_digest(self, digest_id: str) -> dict | None:
        """The server's own row for one digest, for laying an edit over."""
        for row in self._pages(QUERY_DIGESTS, "summaryDOList"):
            if str(row.get("id")) == str(digest_id):
                return row
        return None

    # -- Writing --------------------------------------------------------------

    def create(self, content: str, library_uid: str = "") -> str:
        """Add a digest, and return its new id.

        Only three fields are required. ``uniqueIdentifier`` is ours to choose
        and must not collide with an existing one, so it is a fresh uuid rather
        than anything derived from the text -- two identical notes are two
        notes.
        """
        content = (content or "").strip()
        if not content:
            raise ConnectorError("A digest needs some text.")

        payload = {
            "content": content,
            "uniqueIdentifier": uuid.uuid4().hex,
            "md5Hash": hashlib.md5(content.encode("utf-8")).hexdigest(),
        }
        if library_uid:
            payload["parentUniqueIdentifier"] = library_uid

        body = self._call("POST", ADD_DIGEST, payload)
        new_id = str(body.get("id") or "")
        if not new_id:
            raise ConnectorError("Supernote accepted the digest but returned no id.")
        return new_id

    def update(self, digest_id: str, content: str) -> None:
        """Change a digest's text.

        Sends the server's own row with the new text laid over it, so the
        source path, page reference and handwriting attachment survive an edit
        rather than being blanked by omission. PUT, not POST -- POST on this
        path creates rather than updates.
        """
        content = (content or "").strip()
        if not content:
            raise ConnectorError("A digest needs some text.")

        current = self.raw_digest(digest_id)
        if current is None:
            raise ConnectorError("That digest is no longer on Supernote.")

        payload = dict(current)
        payload["content"] = content
        payload["md5Hash"] = hashlib.md5(content.encode("utf-8")).hexdigest()
        self._call("PUT", UPDATE_DIGEST, payload)

    def delete(self, digest_id: str) -> None:
        """Remove a digest. DELETE with the numeric id, and nothing else works."""
        self._call("DELETE", DELETE_DIGEST, {"id": int(digest_id)})

    def create_library(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise ConnectorError("A library needs a name.")
        body = self._call("POST", ADD_LIBRARY, {
            "name": name,
            "uniqueIdentifier": uuid.uuid4().hex,
            "md5Hash": hashlib.md5(name.encode("utf-8")).hexdigest(),
        })
        return str(body.get("id") or "")

    def delete_library(self, library_id: str) -> None:
        self._call("DELETE", DELETE_LIBRARY, {"id": int(library_id)})

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
