import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def isolated_fetch_ledger():
    """A write-ahead fetch ledger in a throwaway TEMP_DIR directory.

    Every adapter test that reaches ``download_fulltext``'s fetch guard must
    inject one of these onto the adapter (``adapter.fetch_ledger = ledger``),
    so no test ever writes -- or is refused by -- the real audit ledger.
    """

    from hunnu_harness.literature.fetch_ledger import FulltextFetchLedger
    from hunnu_harness.paths import TEMP_DIR

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch-ledger-", dir=TEMP_DIR) as tmp:
        yield FulltextFetchLedger(path=Path(tmp) / "fulltext_fetch_ledger.jsonl")


def minimal_pdf_bytes() -> bytes:
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Length 0 >>\nstream\n\nendstream\nendobj\n",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for item in objects:
        offsets.append(len(content))
        content.extend(item)
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(content)


def write_minimal_pdf(path: Path) -> Path:
    path.write_bytes(minimal_pdf_bytes())
    return path


def minimal_pdf_with_text_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 9 Tf 24 160 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 900 220] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for item in objects:
        offsets.append(len(content))
        content.extend(item)
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(content)


def write_minimal_pdf_with_text(path: Path, text: str) -> Path:
    path.write_bytes(minimal_pdf_with_text_bytes(text))
    return path
