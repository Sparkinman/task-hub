"""Supernote digests: the API shape, and setting them as a PDF.

The API shape is worth pinning down in a test because finding it cost hours to
one wrong assumption. Every one of these endpoints answers a bare "Server
Error" -- no status code, no field name, nothing -- when the paging arguments
are wrong, and the rest of Supernote's API uses ``pageNo``/``pageSize`` where
this controller uses ``page``/``size``. The verbs are the same trap: ``POST`` to
the delete route gives that same unhelpful 500, and only ``DELETE`` works.

Neither of those is discoverable from the response, so if a refactor quietly
changes one, the failure looks like Supernote being down rather than like a
mistake here.

The PDF half is checked by reading the text back out of the file, because a
structurally valid PDF that renders as gibberish passes every other kind of
check. That is not hypothetical: the first version folded newlines into
question marks, and every multi-line digest arrived as one run of text.
"""

from __future__ import annotations

import re
import sys
import zlib

from app.services import supernote_digest as sd
from app.services.text_pdf import Block, _latin, build, width_of, wrap

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


class Recorder(sd.SupernoteDigests):
    """Records the calls a method makes instead of making them."""

    def __init__(self, rows=None, libraries=None):
        super().__init__("token")
        self.calls: list[tuple] = []
        self._rows = rows if rows is not None else []
        self._libraries = libraries or []

    def _call(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == sd.QUERY_DIGESTS:
            return {"summaryDOList": self._rows, "totalPages": 1}
        if path == sd.QUERY_LIBRARIES:
            return {"summaryDOList": self._libraries, "totalPages": 1}
        return {"success": True, "id": 999}


ROW = {
    "id": 882495991503126528,
    "content": "The Beatitudes",
    "parentUniqueIdentifier": "lib-uid",
    "sourcePath": "Document/Bible Stuff/Bible.pdf",
    "sourceType": 1,
    "commentStr": "",
    "commentHandwriteName": "",
    "md5Hash": "b416df427b7cc9aa5f0eada5c72d0c5b",
    "creationTime": 1784728767877,
    "lastModifiedTime": 1784728767877,
    "isDeleted": "N",
    "isSummaryGroup": "N",
    "metadata": '{"document_location_data":"[{\\"chapter\\":0,\\"endPosition\\":190,'
                '\\"page\\":6201,\\"startPosition\\":176}]","source_size":80717759}',
}
LIB = {"id": 801231349322088448, "name": "Bible Notes",
       "uniqueIdentifier": "lib-uid", "isDeleted": "N", "isSummaryGroup": "Y"}

print("\nPaging uses page and size, not pageNo and pageSize")
# The single wrong assumption that made every endpoint look broken.
r = Recorder(rows=[ROW])
r.digests()
_, _, payload = r.calls[0]
check("it sends 'page'", "page" in payload, str(payload))
check("and 'size'", "size" in payload, str(payload))
check("and not pageNo", "pageNo" not in payload, str(payload))
check("and not pageSize", "pageSize" not in payload, str(payload))

print("\nThe verbs are not all POST")
r = Recorder(rows=[ROW])
r.create("hello")
check("create posts", r.calls[-1][0] == "POST", r.calls[-1][0])
r = Recorder(rows=[ROW])
r.update(str(ROW["id"]), "changed")
check("update PUTs", r.calls[-1][0] == "PUT", r.calls[-1][0])
# POST to the delete path answers with the same generic 500 as a wrong payload,
# so falling back to it would look like an outage rather than a mistake.
r = Recorder()
r.delete("123")
method, path, payload = r.calls[-1]
check("delete uses DELETE", method == "DELETE", method)
check("with the numeric id", payload == {"id": 123}, str(payload))

print("\nAn edit keeps what Supernote knows and Task Hub does not")
r = Recorder(rows=[ROW])
r.update(str(ROW["id"]), "a new passage")
_, _, payload = r.calls[-1]
check("the new text is sent", payload["content"] == "a new passage")
check("the checksum is recomputed", payload["md5Hash"] != ROW["md5Hash"])
# Blanking these by sending only what changed would lose the link back to the
# page the passage came from.
check("the source survives", payload["sourcePath"] == ROW["sourcePath"])
check("and so does the location", payload["metadata"] == ROW["metadata"])

print("\nA digest is read into something usable")
digest = Recorder(rows=[ROW]).digests()[0]
check("the passage carries over", digest.content == "The Beatitudes")
check("the page is dug out of the nested metadata", digest.page_number == 6201,
      str(digest.page_number))
check("the file name is separated from its folders",
      digest.source_name == "Bible.pdf", digest.source_name)
check("a library row is not mistaken for a digest",
      Recorder(rows=[ROW, dict(LIB, isSummaryGroup="Y")]).digests().__len__() == 1)
check("a deleted digest is skipped",
      Recorder(rows=[dict(ROW, isDeleted="Y")]).digests() == [])
# Metadata that changes shape should cost the page number, not the digest.
for bad in ("", "not json", '{"document_location_data":"[]"}', "{}"):
    d = Recorder(rows=[dict(ROW, metadata=bad)]).digests()
    check(f"metadata {bad[:16]!r} still yields a digest", len(d) == 1)
    check("  with no page rather than an error", d[0].page_number is None)

print("\nTwo identical notes are two notes")
# The unique identifier must not be derived from the text, or adding the same
# line twice would collide with itself.
r = Recorder()
r.create("same words")
first = r.calls[-1][2]["uniqueIdentifier"]
r.create("same words")
check("each gets its own identifier", first != r.calls[-1][2]["uniqueIdentifier"])
check("but the checksum is of the content",
      r.calls[-1][2]["md5Hash"] == __import__("hashlib").md5(b"same words").hexdigest())

print("\nEmpty text is refused before it reaches Supernote")
for empty in ("", "   ", "\n"):
    try:
        Recorder().create(empty)
        check(f"create({empty!r}) is refused", False, "it was accepted")
    except Exception:
        check(f"create({empty!r}) is refused", True)

print("\nThe PDF sets text a reader can actually get back out")


def text_of(pdf: bytes) -> list[str]:
    lines: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\nendstream", pdf, re.S):
        try:
            raw = zlib.decompress(match.group(1))
        except zlib.error:
            continue
        lines += [t.decode("latin-1") for t in re.findall(rb"\((.*?)\) Tj", raw)]
    return lines


pdf = build("Notes", [
    Block("Notes", size=18, bold=True),
    Block("Line one\nLine two", size=11),
    Block("A passage with “curly quotes” — and an em dash", size=11),
])
check("it is a PDF", pdf.startswith(b"%PDF"))
check("and ends properly", pdf.rstrip().endswith(b"%%EOF"))
lines = text_of(pdf)
# The bug that shipped first: newlines folded to "?" and every multi-line
# digest arrived as one run of text.
check("the author's line breaks survive",
      "Line one" in lines and "Line two" in lines, str(lines))
check("no line contains a stray question mark",
      not any("?" in line for line in lines), str([l for l in lines if "?" in l]))
check("curly quotes become straight ones",
      any('"curly quotes"' in line for line in lines), str(lines))

print("\nEvery cross-reference offset resolves, or a reader refuses the file")
start = int(re.search(rb"startxref\s+(\d+)", pdf).group(1))
check("startxref points at the table", pdf[start:start + 4] == b"xref")
entries = re.findall(rb"(\d{10}) (\d{5}) ([nf])", pdf[start:])
bad = [i for i, (off, _, kind) in enumerate(entries)
       if kind == b"n" and not pdf[int(off):].startswith(f"{i} 0 obj".encode())]
check("every object offset is right", not bad, str(bad))

print("\nLong text is wrapped rather than run off the page")
wrapped = wrap("word " * 60, 11, 300)
check("it produces several lines", len(wrapped) > 1, str(len(wrapped)))
check("and none is wider than the column",
      all(width_of(line, 11) <= 300 for line in wrapped))
# A single unbreakable run must be broken rather than allowed to overflow.
long_word = "x" * 300
check("an unbreakable word is split",
      all(width_of(line, 11) <= 200 for line in wrap(long_word, 11, 200)))

print("\nUnrepresentable characters degrade rather than corrupt")
check("accents are folded", _latin("café") == "cafe", _latin("café"))
check("an emoji becomes a question mark", _latin("hi 🙂") == "hi ?", _latin("hi 🙂"))
check("tabs become spaces", _latin("a\tb") == "a b", repr(_latin("a\tb")))

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll digest tests passed.")
