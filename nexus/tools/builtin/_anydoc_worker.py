"""Bounded, offline AnyDoc document-to-Markdown worker.

Protocol v1 is intentionally small: invoke this module with
``--protocol v1 --extension .pdf`` (or another fixed allowlisted extension),
write the document bytes to stdin, and read a framed UTF-8 Markdown payload from
stdout. The frame is ``NEXUS-ANYDOC-v1\\n``, a four-byte big-endian payload
length, then exactly that many Markdown bytes. Diagnostics are fixed, bounded messages on stderr; document content,
paths, URLs, and exception text are never included.

The worker reads stdin through EOF. Callers must use ``communicate()`` with a
timeout; if it times out, kill the worker and call ``communicate()`` again to
collect its output and finish cleanup.

The optional ``anydoc`` package is imported only when a valid document is
received. Its conversion API is expected to provide ``format_from_bytes``,
``format_from_extension``, and ``to_markdown_bytes`` (which returns Markdown
text). No hosted OCR option is ever passed.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import io
import json
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

PROTOCOL_VERSION = "v1"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_MARKDOWN_BYTES = 4 * 1024 * 1024
MAX_ERROR_BYTES = 240
SUCCESS_MAGIC = b"NEXUS-ANYDOC-v1\n"
SUCCESS_HEADER_BYTES = len(SUCCESS_MAGIC) + 4

SUPPORTED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".docm",
        ".ppt",
        ".pptx",
        ".pptm",
        ".pps",
        ".pot",
        ".ppsx",
        ".ppsm",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".xlsb",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
        ".epub",
        ".csv",
    }
)

_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_ZIP_REQUIRED_MEMBERS: dict[str, str] = {
    ".docx": "word/document.xml",
    ".docm": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".pptm": "ppt/presentation.xml",
    ".ppsx": "ppt/presentation.xml",
    ".ppsm": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
    ".xlsm": "xl/workbook.xml",
    ".xlsb": "xl/workbook.bin",
    ".odt": "content.xml",
    ".ods": "content.xml",
    ".odp": "content.xml",
}


class WorkerError(Exception):
    """A safe, user-visible worker failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _plausible_zip(data: bytes, extension: str) -> bool:
    if not data.startswith(_ZIP_SIGNATURES):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if extension == ".epub":
                if "mimetype" not in names:
                    return False
                info = archive.getinfo("mimetype")
                if info.file_size > 32:
                    return False
                with archive.open(info) as mimetype:
                    return mimetype.read(33) == b"application/epub+zip"
            required = _ZIP_REQUIRED_MEMBERS.get(extension)
            return required is not None and required in names
    except (OSError, ValueError, zipfile.BadZipFile, KeyError, RuntimeError):
        return False


def _plausible_input(data: bytes, extension: str) -> bool:
    """Reject obviously malformed data before AnyDoc's extension fallback."""
    if not data:
        return False
    if extension == ".pdf":
        return data.startswith(b"%PDF-")
    if extension in {".doc", ".ppt", ".pps", ".pot", ".xls"}:
        return data.startswith(_OLE_SIGNATURE)
    if extension in _ZIP_REQUIRED_MEMBERS or extension == ".epub":
        return _plausible_zip(data, extension)
    if extension == ".rtf":
        return data.lstrip(b"\xef\xbb\xbf\t\r\n ").startswith(b"{\\rtf")
    if extension == ".csv":
        # CSV encodings are not reliably identifiable from their bytes alone;
        # leave decoding and validation to AnyDoc (including UTF-16 input).
        return bool(data)
    return False


def _load_anydoc():
    try:
        distribution = importlib.metadata.distribution("firecrawl-anydoc")
    except importlib.metadata.PackageNotFoundError:
        raise WorkerError(
            "ANYDOC_UNAVAILABLE",
            "Firecrawl AnyDoc is unavailable; install the documents extra",
        ) from None
    distribution_name = distribution.metadata.get("Name", "")
    if distribution_name.lower().replace("_", "-") != "firecrawl-anydoc":
        raise WorkerError(
            "ANYDOC_UNAVAILABLE",
            "the installed anydoc module is not from Firecrawl AnyDoc",
        )
    try:
        anydoc = importlib.import_module("anydoc")
    except ImportError as exc:
        raise WorkerError(
            "ANYDOC_UNAVAILABLE",
            "Firecrawl AnyDoc is installed but its anydoc module is unavailable",
        ) from exc
    module_path = getattr(anydoc, "__file__", None)
    distribution_files = distribution.files
    if module_path is None:
        raise WorkerError(
            "ANYDOC_UNAVAILABLE",
            "the imported anydoc module cannot be verified as Firecrawl AnyDoc",
        )

    module_location = Path(module_path).resolve()
    if distribution_files is not None:
        module_record = next(
            (
                file
                for file in distribution_files
                if str(file).replace("\\", "/") == "anydoc/__init__.py"
            ),
            None,
        )
        verified_location = (
            module_record is not None
            and module_location == Path(distribution.locate_file(module_record)).resolve()
        )
    else:
        # PEP 660 editable installs may have no RECORD file list. In that case,
        # require both the installed metadata's package ownership mapping and
        # its editable project URL to agree with the imported package location.
        # The worker runs isolated (-I), so the editable finder—not the current
        # working directory—must resolve this module.
        try:
            providers = importlib.metadata.packages_distributions().get("anydoc", ())
            owns_package = any(
                provider.lower().replace("_", "-") == "firecrawl-anydoc"
                for provider in providers
            )
            direct_url = distribution.read_text("direct_url.json")
            direct_url_data = json.loads(direct_url) if direct_url is not None else {}
            parsed_url = urlparse(direct_url_data.get("url", ""))
            project_root = Path(url2pathname(parsed_url.path)).resolve()
            relative_module_path = module_location.relative_to(project_root)
            editable_project = (
                direct_url_data.get("dir_info", {}).get("editable") is True
                and parsed_url.scheme == "file"
                and not parsed_url.netloc
                and relative_module_path.parts[-2:] == ("anydoc", "__init__.py")
            )
            verified_location = owns_package and editable_project
        except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
            verified_location = False

    if not verified_location:
        raise WorkerError(
            "ANYDOC_UNAVAILABLE",
            "the imported anydoc module is not provided by Firecrawl AnyDoc",
        )
    if not all(
        callable(getattr(anydoc, name, None))
        for name in ("format_from_bytes", "format_from_extension", "to_markdown_bytes")
    ):
        raise WorkerError(
            "ANYDOC_UNAVAILABLE",
            "the installed anydoc module does not provide Firecrawl's conversion API",
        )
    return anydoc


def convert(data: bytes, extension: str) -> bytes:
    """Convert validated bytes to bounded UTF-8 Markdown bytes."""
    if extension not in SUPPORTED_EXTENSIONS:
        raise WorkerError("UNSUPPORTED_FORMAT", "unsupported document extension")
    if not data:
        raise WorkerError("BAD_INPUT", "document input is empty or invalid")
    if len(data) > MAX_INPUT_BYTES:
        raise WorkerError("INPUT_TOO_LARGE", "document exceeds the input byte limit")
    if not _plausible_input(data, extension):
        raise WorkerError("BAD_INPUT", "document input is empty or invalid")

    anydoc = _load_anydoc()
    try:
        detected_format = anydoc.format_from_bytes(data)
        selected_format = detected_format
        if selected_format is None or selected_format is False or selected_format == "":
            # Extension fallback is used only after extension-specific input
            # validation above, never as a way to make arbitrary bytes parseable.
            selected_format = anydoc.format_from_extension(extension)
        markdown = anydoc.to_markdown_bytes(data, selected_format)
    except Exception as exc:  # noqa: BLE001 - AnyDoc defines its own conversion errors.
        if type(exc).__name__ == "NeedsOcrError":
            raise WorkerError("OCR_REQUIRED", "document requires OCR; hosted OCR is disabled") from None
        raise WorkerError("CONVERSION_FAILED", "document conversion failed") from None

    if isinstance(markdown, str):
        try:
            markdown_bytes = markdown.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkerError("CONVERSION_FAILED", "anydoc returned invalid Markdown text") from None
    elif isinstance(markdown, bytes):
        # Accept bytes for forward compatibility with alternate AnyDoc versions.
        markdown_bytes = markdown
    else:
        raise WorkerError("CONVERSION_FAILED", "anydoc returned invalid Markdown text")
    if len(markdown_bytes) > MAX_MARKDOWN_BYTES:
        raise WorkerError("OUTPUT_TOO_LARGE", "Markdown exceeds the output byte limit")
    try:
        markdown_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkerError("CONVERSION_FAILED", "anydoc returned non-UTF-8 Markdown") from None
    return markdown_bytes


def _extension_from_args(argv: Sequence[str]) -> str:
    if len(argv) != 4 or argv[0] != "--protocol" or argv[1] != PROTOCOL_VERSION or argv[2] != "--extension":
        raise WorkerError("BAD_PROTOCOL", "expected --protocol v1 --extension EXT")
    extension = argv[3].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise WorkerError("UNSUPPORTED_FORMAT", "unsupported document extension")
    return extension


def _write_error(error: WorkerError) -> None:
    line = f"ERROR[{error.code}]: {error.message}\n".encode("ascii")
    sys.stderr.buffer.write(line[:MAX_ERROR_BYTES])
    sys.stderr.buffer.flush()


def _write_success(markdown: bytes) -> None:
    header = SUCCESS_MAGIC + len(markdown).to_bytes(4, "big")
    sys.stdout.buffer.write(header + markdown)
    sys.stdout.buffer.flush()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        extension = _extension_from_args(sys.argv[1:] if argv is None else argv)
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        markdown = convert(data, extension)
        _write_success(markdown)
        return 0
    except WorkerError as exc:
        _write_error(exc)
        return 2
    except Exception:  # noqa: BLE001 - keep unexpected worker failures content-free.
        # Keep unexpected failures content-free and bounded as well.
        _write_error(WorkerError("WORKER_FAILED", "document worker failed"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
