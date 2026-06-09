"""Per-artifact extraction — turn an uploaded work product into engine-readable
structure, keyed on artifact KIND (pdf | spreadsheet | doc | …), never on workflow
type. Kinds are extensible: a new workflow touching the same kind reuses the same
extractor.

Stdlib only (csv, zipfile, xml, zlib) so it runs offline with no new deps. Each
extractor recovers what a builder needs from that kind:
  - spreadsheet : header row, a few real rows, tab names, detected file format
  - doc         : extracted text + field layout (label/value pairs)
  - pdf         : extracted text (best-effort) + field layout

Extracted text is redacted at write time (secrets/PII), the same posture as
captured content; the raw file is retained and referenced, not inlined blindly.
"""

import csv
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

from ..capture.redaction import redact_text

_MAX_ROWS = 5
_MAX_TEXT = 4000

_SPREADSHEET_EXT = {".csv", ".tsv", ".xlsx", ".xls", ".ods", ".numbers"}
_DOC_EXT = {".txt", ".md", ".rtf", ".docx", ".doc"}
_PDF_EXT = {".pdf"}


def detect_kind(filename, data=b""):
    """Classify a file into an artifact kind from its extension, with a magic-byte
    fallback. Returns 'pdf' | 'spreadsheet' | 'doc' | 'unknown'."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _PDF_EXT:
        return "pdf"
    if ext in _SPREADSHEET_EXT:
        return "spreadsheet"
    if ext in _DOC_EXT:
        return "doc"
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:2] == b"PK":  # zip container: xlsx vs docx
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
            if any(n.startswith("xl/") for n in names):
                return "spreadsheet"
            if any(n.startswith("word/") for n in names):
                return "doc"
        except zipfile.BadZipFile:
            pass
    if data and b"," in data.split(b"\n", 1)[0]:
        return "spreadsheet"
    return "unknown"


def _fmt(filename):
    return (os.path.splitext(filename or "")[1].lower().lstrip(".")) or "unknown"


def _redact_cells(rows):
    return [[redact_text(str(c)) if c is not None else "" for c in row] for row in rows]


def _field_layout(text):
    """Recover label/value pairs ('Label: value' lines) generically."""
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z][\w /&-]{1,40}?)\s*[:\-]\s+(.+)", line)
        if m:
            out.append({"label": redact_text(m.group(1).strip()),
                        "value": redact_text(m.group(2).strip())})
    # also catch inline "Label: value" patterns when text has no newlines
    if not out:
        for m in re.finditer(r"([A-Za-z][\w /&-]{1,30}?):\s*([^\n:]{1,60})", text):
            out.append({"label": redact_text(m.group(1).strip()),
                        "value": redact_text(m.group(2).strip())})
    return out[:25]


# --- spreadsheet ----------------------------------------------------------
def _local(tag):
    return tag.split("}")[-1]


def _extract_xlsx(path):
    z = zipfile.ZipFile(path)
    names = z.namelist()
    tabs = []
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        tabs = [s.get("name") for s in wb.iter() if _local(s.tag) == "sheet"]
    except (KeyError, ET.ParseError):
        pass
    shared = []
    if "xl/sharedStrings.xml" in names:
        try:
            ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in ss:
                if _local(si.tag) == "si":
                    shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
        except ET.ParseError:
            pass
    sheet_name = next((n for n in names if re.match(r"xl/worksheets/sheet1\.xml$", n)), None) \
        or next((n for n in names if n.startswith("xl/worksheets/")), None)
    rows = []
    if sheet_name:
        try:
            sheet = ET.fromstring(z.read(sheet_name))
            for row in (e for e in sheet.iter() if _local(e.tag) == "row"):
                cells = []
                for c in (e for e in row if _local(e.tag) == "c"):
                    val = ""
                    if c.get("t") == "inlineStr":  # inline string: <is><t>…</t></is>
                        val = "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
                    else:
                        v = next((e for e in c if _local(e.tag) == "v"), None)
                        if v is not None and v.text is not None:
                            val = (shared[int(v.text)] if (c.get("t") == "s" and v.text.isdigit()
                                   and int(v.text) < len(shared)) else v.text)
                    cells.append(val)
                rows.append(cells)
                if len(rows) > _MAX_ROWS + 1:
                    break
        except (KeyError, ET.ParseError):
            pass
    headers = rows[0] if rows else []
    return {"format": "xlsx", "tab_names": tabs or ["(unknown)"],
            "headers": _redact_cells([headers])[0] if headers else [],
            "sample_rows": _redact_cells(rows[1:_MAX_ROWS + 1])}


def _extract_spreadsheet(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        try:
            return _extract_xlsx(path)
        except zipfile.BadZipFile:
            pass
    if ext in (".csv", ".tsv", ""):
        delim = "\t" if ext == ".tsv" else ","
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            rows = list(csv.reader(f, delimiter=delim))[: _MAX_ROWS + 1]
        headers = rows[0] if rows else []
        return {"format": (ext.lstrip(".") or "csv"), "tab_names": ["(single sheet)"],
                "headers": _redact_cells([headers])[0] if headers else [],
                "sample_rows": _redact_cells(rows[1:])}
    # binary spreadsheet kinds we can't parse with stdlib: surface the FORMAT
    # (this is the .xlsx-vs-native fact that prevents a format mismatch downstream)
    return {"format": _fmt(path), "tab_names": [], "headers": [], "sample_rows": [],
            "note": "format detected; headers not extracted for this format without a parser"}


# --- doc ------------------------------------------------------------------
def _extract_docx(path):
    z = zipfile.ZipFile(path)
    text = ""
    try:
        doc = ET.fromstring(z.read("word/document.xml"))
        text = " ".join(t.text or "" for t in doc.iter() if _local(t.tag) == "t")
    except (KeyError, ET.ParseError):
        pass
    return text


def _extract_doc(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        text = _extract_docx(path)
        fmt = "docx"
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        fmt = _fmt(path)
    return {"format": fmt, "text": redact_text(text[:_MAX_TEXT]),
            "field_layout": _field_layout(text)}


# --- pdf (best-effort, no external library) -------------------------------
def _pdf_text(data):
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        raw = m.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        for t in re.findall(rb"\((?:[^()\\]|\\.)*\)", raw):
            chunks.append(t[1:-1])
    text = " ".join(c.decode("latin-1", "ignore") for c in chunks)
    return re.sub(r"\\[nrt]", " ", text)


def _extract_pdf(path):
    with open(path, "rb") as f:
        data = f.read()
    text = _pdf_text(data)
    note = None
    if not text.strip():
        note = "no embedded text recovered (likely image/scanned PDF); format detected"
    return {"format": "pdf", "text": redact_text(text[:_MAX_TEXT]),
            "field_layout": _field_layout(text), **({"note": note} if note else {})}


_EXTRACTORS = {
    "spreadsheet": _extract_spreadsheet,
    "doc": _extract_doc,
    "pdf": _extract_pdf,
}


def extract(kind, path):
    """Extract engine-readable structure for a file of the given kind. Unknown
    kinds yield a minimal record (format only) rather than failing."""
    fn = _EXTRACTORS.get(kind)
    if fn is None:
        return {"format": _fmt(path), "note": f"no extractor for kind {kind!r}"}
    try:
        return fn(path)
    except Exception as e:  # never let a malformed upload crash the pipeline
        return {"format": _fmt(path), "note": f"extraction failed: {type(e).__name__}: {e}"}
