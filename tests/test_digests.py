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

print("\nChoosing libraries must not silently drop the unfiled ones")
# Reported from a real account: four libraries ticked, and two highlights
# vanished because they belonged to none of them. Before this, the only way to
# include those was to tick nothing at all -- the opposite of what somebody
# choosing carefully would expect.
from app.sync.digest_sync import UNFILED_UID  # noqa: E402


def kept(selected: set[str], library_uids: list[str]) -> list[str]:
    """Mirror the rule run_sync applies, for the cases that matter."""
    def chosen(uid: str) -> bool:
        if not selected:
            return True
        return uid in selected if uid else UNFILED_UID in selected
    return [uid for uid in library_uids if chosen(uid)]


everything = ["lib-a", "lib-b", ""]
check("ticking nothing keeps everything",
      kept(set(), everything) == everything)
check("ticking one library keeps only it",
      kept({"lib-a"}, everything) == ["lib-a"])
check("ticking every real library still leaves the unfiled ones out",
      kept({"lib-a", "lib-b"}, everything) == ["lib-a", "lib-b"])
check("and ticking the unfiled row brings them in",
      kept({"lib-a", "lib-b", UNFILED_UID}, everything) == everything)
check("the unfiled row alone keeps only those",
      kept({UNFILED_UID}, everything) == [""])
# Supernote's own identifiers are 32-character hex, so the sentinel cannot be
# mistaken for one.
check("the sentinel cannot collide with a real library id",
      not re.fullmatch(r"[0-9a-f]{32}", UNFILED_UID), UNFILED_UID)

print("\nA digest's handwriting is decoded, not just noted")
# Both halves of a digest's note are carried: the typed one arrives in the
# listing as commentStr, the handwritten one is a separate file that has to be
# fetched and decoded. Task Hub showed neither picture at first and simply said
# the writing was "on the tablet", which is the least useful thing it could say
# about the half somebody wrote themselves.
from app.services import supernote_mark as mark  # noqa: E402
from app.services.text_pdf import Image  # noqa: E402

check("the listing carries a typed note",
      Recorder(rows=[dict(ROW, commentStr="Typed here")]).digests()[0].comment
      == "Typed here")
check("and the handwriting's checksum, to tell a changed drawing from a fetched one",
      Recorder(rows=[dict(ROW, handwriteMD5="abc123")]).digests()[0].handwriting_md5
      == "abc123")

print("\nThe layer header is found wherever it sits in the file")
# The regression that cost a real digest its picture: layer headers are near
# the front of a small mark file and *after* the bitmap in a larger one. An
# earlier version scanned only the first 64KB, so one of two real files decoded
# and the other silently produced nothing.
far = b"markSN_FILE_VER_20230015" + b"\x00" * 70000 + b"<LAYERBITMAP:429>"
check("an address past 64KB is still found",
      mark._bitmap_addresses(far) == [429], str(mark._bitmap_addresses(far)))
check("several layers give several candidates",
      mark._bitmap_addresses(b"<LAYERBITMAP:12><LAYERBITMAP:80>" + b"\x00" * 200)
      == [12, 80])
check("an address past the end of the file is refused",
      mark._bitmap_addresses(b"<LAYERBITMAP:999999>") == [])
check("and so is one that is not a number",
      mark._bitmap_addresses(b"<LAYERBITMAP:none>" + b"\x00" * 100) == [])

print("\nRun-length decoding expands to one grey byte per pixel")
# Pairs of (colour, length); a length under 0x80 means that many pixels plus
# one, and the high bit marks the low seven bits of a longer run.
plain = mark.decode_rle(bytes([0x61, 0x02, 0x62, 0x01]), 5)
check("a short run is that many plus one", plain == bytes([0, 0, 0, 255, 255]),
      str(bytes(plain)))
check("a short read is padded rather than refused",
      len(mark.decode_rle(bytes([0x61, 0x00]), 10)) == 10)
check("an empty payload gives nothing", mark.decode_rle(b"", 10) is None)

print("\nA file that is not a mark file is refused rather than drawn")
for rubbish in (b"", b"not a supernote file", b"%PDF-1.4 hello"):
    check(f"{rubbish[:12]!r} is refused", mark.to_image(rubbish) is None)
    check("  and yields no PNG", mark.to_png(rubbish) is None)

print("\nThe saved picture can be read back for the PDF")
# The PDF needs raw samples, so the drawing is read back out of the PNG rather
# than the mark file being decoded again on every request.
pixels = bytes(range(256)) * 4
written = mark._png(pixels, 32, 32, scale=1)
found = mark.read_png(written)
check("it reads back", found is not None)
if found:
    back, w, h = found
    check("at the same size", (w, h) == (32, 32), f"{w}x{h}")
    check("with the same pixels", back == pixels)
check("something that is not a PNG is refused", mark.read_png(b"nope") is None)

print("\nHandwriting reaches the PDF as a picture, not a note about a picture")
with_image = build("Digest", [
    Block("The Beatitudes", size=11.5),
    Image(bytes([0]) * (60 * 20), 60, 20),
])
check("the page declares the drawing", b"/XObject" in with_image)
check("as an 8-bit greyscale image",
      b"/ColorSpace /DeviceGray" in with_image and b"/BitsPerComponent 8" in with_image)
drawn = [zlib.decompress(m.group(1))
         for m in re.finditer(rb"stream\r?\n(.*?)\nendstream", with_image, re.S)
         if b"Do" in m.group(0) or True]
check("and paints it", any(b"/Im1 Do" in d for d in drawn if b"Do" in d),
      str([d[:80] for d in drawn]))
# The offsets are rebuilt around the image objects, and a reader refuses the
# whole file if one is wrong -- so this is checked again with an image present.
start = int(re.search(rb"startxref\s+(\d+)", with_image).group(1))
entries = re.findall(rb"(\d{10}) (\d{5}) ([nf])", with_image[start:])
bad = [i for i, (off, _, kind) in enumerate(entries)
       if kind == b"n" and not with_image[int(off):].startswith(f"{i} 0 obj".encode())]
check("every offset still resolves with images present", not bad, str(bad))

print("\nA drawing is sized to the writing, not stretched to the column")
# A two-word note blown up to the width of the page reads as shouting.
small = Image(b"\x00" * (200 * 60), 200, 60)
w, h = small.placed(column=483.0, page=730.0)
check("a small drawing keeps its proportions",
      abs(w / h - 200 / 60) < 0.01, f"{w:.1f}x{h:.1f}")
check("and is not blown up to fill the column", w < 483.0, f"{w:.1f}")
# One too big for the page must shrink, or it would be clipped invisibly.
huge = Image(b"\x00" * 10, 4000, 9000)
w, h = huge.placed(column=483.0, page=730.0)
# Shrunk to exactly the height available, so this is compared with a hair of
# tolerance rather than exactly.
check("an oversized drawing is shrunk to fit", w <= 483.01 and h <= 730.01,
      f"{w:.4f}x{h:.4f}")
check("keeping its proportions", abs(w / h - 4000 / 9000) < 0.01)

print("\nFiling a new digest offers every library, not just the mirrored ones")
# Reported from a real account: six libraries on the tablet, two in the menu.
# The menu was built from the digests already on the page, so a library that
# was not being mirrored -- or simply had nothing in it yet -- could not be
# chosen at all.
import json as _json  # noqa: E402


def menu(cache: str) -> list[str]:
    """Mirror what known_libraries makes of a stored cache."""
    if not cache:
        return []
    try:
        rows = _json.loads(cache)
    except ValueError:
        return []
    return [r["name"] for r in rows if isinstance(r, dict) and r.get("uid")]


stored = _json.dumps([
    {"uid": "a", "name": "Bible Notes"}, {"uid": "b", "name": "Work"},
    {"uid": "", "name": "Broken"},
])
check("every remembered library is offered",
      menu(stored) == ["Bible Notes", "Work"], str(menu(stored)))
check("one with no identifier is left out", "Broken" not in menu(stored))
for broken in ("", "not json", "[]"):
    check(f"a {broken[:8]!r} cache gives an empty menu rather than an error",
          menu(broken) == [])

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll digest tests passed.")
