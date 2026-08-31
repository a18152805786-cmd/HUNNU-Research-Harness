from __future__ import annotations

import re
from pathlib import Path

from ..paths import _windows_io_path
from .models import PDFValidationResult, UNKNOWN


class PDFValidator:
    """Validate downloaded content before it can enter a success manifest."""

    @staticmethod
    def validate(path: Path) -> PDFValidationResult:
        path = Path(path)
        path_io = _windows_io_path(path)
        if not path_io.exists() or not path_io.is_file():
            return PDFValidationResult(path, False, False, False, False, None, 0, "File does not exist")
        try:
            size = path_io.stat().st_size
        except OSError as exc:
            return PDFValidationResult(path, False, False, False, False, None, 0, str(exc))
        if size <= 0:
            return PDFValidationResult(path, True, False, False, False, None, size, "File is empty")

        try:
            with path_io.open("rb") as handle:
                header = handle.read(1024)
        except OSError as exc:
            return PDFValidationResult(path, True, True, False, False, None, size, str(exc))
        header_valid = header.startswith(b"%PDF-")
        if not header_valid:
            error = "HTML response detected" if b"<html" in header.lower() else "Missing %PDF header"
            return PDFValidationResult(path, True, True, False, False, None, size, error)

        readable: bool | None = None
        page_count: int | None = None
        error = UNKNOWN
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]

            with path_io.open("rb") as handle:
                reader = PdfReader(handle, strict=False)
                page_count = len(reader.pages)
            readable = page_count > 0
            if not readable:
                error = "PDF contains no readable pages"
        except ImportError:
            try:
                data = path_io.read_bytes()
                matches = re.findall(rb"/Type\s*/Page(?!s)\b", data)
                if matches:
                    page_count = len(matches)
                    readable = True
            except OSError as exc:
                readable = False
                error = str(exc)
        except Exception as exc:
            readable = False
            error = f"PDF parser rejected file: {type(exc).__name__}"

        return PDFValidationResult(path, True, True, True, readable, page_count, size, error)
