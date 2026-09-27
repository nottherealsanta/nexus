"""Offline tests for the isolated AnyDoc worker contract."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

_WORKER_PATH = Path(__file__).parents[1] / "nexus" / "tools" / "builtin" / "_anydoc_worker.py"
_WORKER_SPEC = importlib.util.spec_from_file_location("_anydoc_worker_test_target", _WORKER_PATH)
assert _WORKER_SPEC is not None and _WORKER_SPEC.loader is not None
worker = importlib.util.module_from_spec(_WORKER_SPEC)
_WORKER_SPEC.loader.exec_module(worker)
_DEFAULT_FILES = object()


def _zip_document(member: str, content: bytes = b"document") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(member, content)
    return output.getvalue()


def _fake_anydoc(
    monkeypatch, *, detected="pdf", output="# converted\n", files=_DEFAULT_FILES
):
    calls: list[tuple] = []
    module = types.ModuleType("anydoc")
    module.__file__ = "/fake/site-packages/anydoc/__init__.py"
    distribution = types.SimpleNamespace(
        metadata={"Name": "firecrawl-anydoc"},
        files=[Path("anydoc/__init__.py")] if files is _DEFAULT_FILES else files,
        locate_file=lambda file: Path("/fake/site-packages") / file,
    )
    monkeypatch.setattr(worker.importlib.metadata, "distribution", lambda name: distribution)

    def format_from_bytes(data):
        calls.append(("detect", data))
        return detected

    def format_from_extension(extension):
        calls.append(("extension", extension))
        return extension.removeprefix(".")

    def to_markdown_bytes(data, fmt, **kwargs):
        calls.append(("convert", data, fmt, kwargs))
        return output

    module.format_from_bytes = format_from_bytes
    module.format_from_extension = format_from_extension
    module.to_markdown_bytes = to_markdown_bytes
    monkeypatch.setitem(sys.modules, "anydoc", module)
    return calls


def test_signature_detection_and_conversion_use_detected_format(monkeypatch):
    data = b"%PDF-1.7\nbody"
    calls = _fake_anydoc(monkeypatch)

    assert worker.convert(data, ".pdf") == b"# converted\n"
    assert calls == [
        ("detect", data),
        ("convert", data, "pdf", {}),
    ]


def test_extension_fallback_after_validated_signature(monkeypatch):
    data = _zip_document("word/document.xml")
    calls = _fake_anydoc(monkeypatch, detected=None)

    assert worker.convert(data, ".docx") == b"# converted\n"
    assert calls == [
        ("detect", data),
        ("extension", ".docx"),
        ("convert", data, "docx", {}),
    ]


@pytest.mark.parametrize("extension", [".exe", "https://example.invalid/file.pdf", "../../secret.pdf"])
def test_rejects_unsupported_extensions_before_import(monkeypatch, extension):
    monkeypatch.delitem(sys.modules, "anydoc", raising=False)
    with pytest.raises(worker.WorkerError, match="unsupported document extension") as exc:
        worker.convert(b"%PDF-1.7\n", extension)
    assert exc.value.code == "UNSUPPORTED_FORMAT"


def test_rejects_bad_signature_even_with_allowed_extension(monkeypatch):
    _fake_anydoc(monkeypatch)
    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"not a PDF", ".pdf")
    assert exc.value.code == "BAD_INPUT"


def test_unavailable_optional_dependency_is_explicit(monkeypatch):
    # An unrelated importable module must not make the dependency look present.
    monkeypatch.setitem(sys.modules, "anydoc", types.ModuleType("anydoc"))
    monkeypatch.setattr(
        worker.importlib.metadata,
        "distribution",
        lambda name: (_ for _ in ()).throw(worker.importlib.metadata.PackageNotFoundError(name)),
    )
    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"%PDF-1.7\n", ".pdf")
    assert exc.value.code == "ANYDOC_UNAVAILABLE"
    assert "Firecrawl AnyDoc" in exc.value.message


def test_rejects_unrelated_distribution_providing_anydoc_module(monkeypatch):
    _fake_anydoc(monkeypatch)
    distribution = types.SimpleNamespace(metadata={"Name": "anydoc"})
    monkeypatch.setattr(worker.importlib.metadata, "distribution", lambda name: distribution)

    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"%PDF-1.7\n", ".pdf")
    assert exc.value.code == "ANYDOC_UNAVAILABLE"
    assert "not from Firecrawl AnyDoc" in exc.value.message


def test_rejects_anydoc_module_outside_firecrawl_distribution(monkeypatch):
    _fake_anydoc(monkeypatch)
    module = types.ModuleType("anydoc")
    module.__file__ = "/other/site-packages/anydoc/__init__.py"
    module.format_from_bytes = lambda data: "pdf"
    module.format_from_extension = lambda extension: "pdf"
    module.to_markdown_bytes = lambda data, fmt: "# converted\n"
    monkeypatch.setitem(sys.modules, "anydoc", module)

    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"%PDF-1.7\n", ".pdf")
    assert exc.value.code == "ANYDOC_UNAVAILABLE"
    assert "not provided by Firecrawl AnyDoc" in exc.value.message


def test_loads_editable_firecrawl_distribution_without_file_records(
    monkeypatch, tmp_path: Path
):
    _fake_anydoc(monkeypatch, files=None)
    project = tmp_path / "firecrawl-anydoc"
    module = sys.modules["anydoc"]
    module.__file__ = str(project / "src" / "anydoc" / "__init__.py")
    distribution = types.SimpleNamespace(
        metadata={"Name": "firecrawl-anydoc"},
        files=None,
        read_text=lambda name: json.dumps(
            {"url": project.as_uri(), "dir_info": {"editable": True}}
        ),
    )
    monkeypatch.setattr(worker.importlib.metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(
        worker.importlib.metadata,
        "packages_distributions",
        lambda: {"anydoc": ["firecrawl-anydoc"]},
    )

    assert worker._load_anydoc() is module


def test_rejects_unverified_module_without_file_records(monkeypatch, tmp_path: Path):
    _fake_anydoc(monkeypatch, files=None)
    project = tmp_path / "firecrawl-anydoc"
    module = sys.modules["anydoc"]
    module.__file__ = str(tmp_path / "workspace" / "anydoc" / "__init__.py")
    distribution = types.SimpleNamespace(
        metadata={"Name": "firecrawl-anydoc"},
        files=None,
        read_text=lambda name: json.dumps(
            {"url": project.as_uri(), "dir_info": {"editable": True}}
        ),
    )
    monkeypatch.setattr(worker.importlib.metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(
        worker.importlib.metadata,
        "packages_distributions",
        lambda: {"anydoc": ["firecrawl-anydoc"]},
    )

    with pytest.raises(worker.WorkerError) as exc:
        worker._load_anydoc()
    assert exc.value.code == "ANYDOC_UNAVAILABLE"
    assert "not provided by Firecrawl AnyDoc" in exc.value.message


def test_input_size_is_bounded_before_optional_import(monkeypatch):
    monkeypatch.delitem(sys.modules, "anydoc", raising=False)
    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"%PDF-1.7\n" + b"x" * worker.MAX_INPUT_BYTES, ".pdf")
    assert exc.value.code == "INPUT_TOO_LARGE"


def test_markdown_output_size_is_bounded(monkeypatch):
    _fake_anydoc(monkeypatch, output="x" * (worker.MAX_MARKDOWN_BYTES + 1))
    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"%PDF-1.7\n", ".pdf")
    assert exc.value.code == "OUTPUT_TOO_LARGE"


def test_markdown_output_limit_counts_utf8_bytes(monkeypatch):
    _fake_anydoc(monkeypatch, output="é" * (worker.MAX_MARKDOWN_BYTES // 2 + 1))
    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"%PDF-1.7\n", ".pdf")
    assert exc.value.code == "OUTPUT_TOO_LARGE"


def test_markdown_output_is_utf8_encoded_and_exact_limit_is_allowed(monkeypatch):
    expected = "é" * (worker.MAX_MARKDOWN_BYTES // 2)
    _fake_anydoc(monkeypatch, output=expected)

    assert worker.convert(b"%PDF-1.7\n", ".pdf") == expected.encode("utf-8")


def test_bytes_markdown_is_accepted_for_forward_compatibility(monkeypatch):
    _fake_anydoc(monkeypatch, output=b"# converted\n")

    assert worker.convert(b"%PDF-1.7\n", ".pdf") == b"# converted\n"


def test_csv_utf16_is_delegated_to_anydoc_with_extension_fallback(monkeypatch):
    data = "name,value\nZoë,1\n".encode("utf-16")
    calls = _fake_anydoc(monkeypatch, detected=None)

    assert worker.convert(data, ".csv") == b"# converted\n"
    assert calls == [
        ("detect", data),
        ("extension", ".csv"),
        ("convert", data, "csv", {}),
    ]


def test_conversion_never_requests_hosted_ocr(monkeypatch):
    data = b"%PDF-1.7\n"
    calls = _fake_anydoc(monkeypatch)

    worker.convert(data, ".pdf")

    convert_call = next(call for call in calls if call[0] == "convert")
    assert convert_call[3] == {}


def test_ocr_requirement_is_explicit_and_never_hosted(monkeypatch):
    _fake_anydoc(monkeypatch)
    module = types.ModuleType("anydoc")
    module.__file__ = "/fake/site-packages/anydoc/__init__.py"
    module.format_from_bytes = lambda data: "pdf"
    module.format_from_extension = lambda extension: "pdf"

    def require_ocr(data, fmt):
        raise type("NeedsOcrError", (Exception,), {})()

    module.to_markdown_bytes = require_ocr
    monkeypatch.setitem(sys.modules, "anydoc", module)

    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"%PDF-1.7\n", ".pdf")
    assert exc.value.code == "OCR_REQUIRED"
    assert "hosted OCR is disabled" in exc.value.message


def test_ocr_words_in_other_errors_do_not_classify_as_ocr(monkeypatch):
    _fake_anydoc(monkeypatch)
    module = types.ModuleType("anydoc")
    module.__file__ = "/fake/site-packages/anydoc/__init__.py"
    module.format_from_bytes = lambda data: "pdf"
    module.format_from_extension = lambda extension: "pdf"

    def conversion_failure(data, fmt):
        raise RuntimeError("OCR required")

    module.to_markdown_bytes = conversion_failure
    monkeypatch.setitem(sys.modules, "anydoc", module)

    with pytest.raises(worker.WorkerError) as exc:
        worker.convert(b"%PDF-1.7\n", ".pdf")
    assert exc.value.code == "CONVERSION_FAILED"


@pytest.mark.parametrize(
    "extension,member",
    [
        (".pptm", "ppt/presentation.xml"),
        (".ppsx", "ppt/presentation.xml"),
        (".ppsm", "ppt/presentation.xml"),
    ],
)
def test_accepts_plausible_powerpoint_zip_variants(extension, member, monkeypatch):
    data = _zip_document(member)
    _fake_anydoc(monkeypatch)

    assert worker.convert(data, extension) == b"# converted\n"


@pytest.mark.parametrize("extension", [".pps", ".pot"])
def test_accepts_ole_powerpoint_variants(extension, monkeypatch):
    _fake_anydoc(monkeypatch)

    assert worker.convert(worker._OLE_SIGNATURE + b"legacy", extension) == b"# converted\n"


def test_cli_protocol_reads_only_stdin_and_writes_framed_markdown(monkeypatch):
    _fake_anydoc(monkeypatch)
    stdin = io.BytesIO(b"%PDF-1.7\n")
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    monkeypatch.setattr(worker.sys, "stdin", types.SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(worker.sys, "stdout", types.SimpleNamespace(buffer=stdout))
    monkeypatch.setattr(worker.sys, "stderr", types.SimpleNamespace(buffer=stderr))

    assert worker.main(["--protocol", "v1", "--extension", ".pdf"]) == 0
    assert stdout.getvalue() == worker.SUCCESS_MAGIC + (12).to_bytes(4, "big") + b"# converted\n"
    assert stderr.getvalue() == b""


def test_cli_native_stdout_noise_precedes_frame_and_is_not_sanitized(monkeypatch):
    _fake_anydoc(monkeypatch)
    stdin = io.BytesIO(b"%PDF-1.7\n")
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    monkeypatch.setattr(worker.sys, "stdin", types.SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(worker.sys, "stdout", types.SimpleNamespace(buffer=stdout))
    monkeypatch.setattr(worker.sys, "stderr", types.SimpleNamespace(buffer=stderr))

    def noisy_convert(data, fmt, **kwargs):
        worker.sys.stdout.buffer.write(b"native warning\n")
        return b"# converted\n"

    sys.modules["anydoc"].to_markdown_bytes = noisy_convert
    assert worker.main(["--protocol", "v1", "--extension", ".pdf"]) == 0
    assert stdout.getvalue().startswith(b"native warning\n" + worker.SUCCESS_MAGIC)


def test_cli_rejects_path_argument_without_echoing_it(monkeypatch):
    stderr = io.BytesIO()
    monkeypatch.setattr(worker.sys, "stderr", types.SimpleNamespace(buffer=stderr))

    assert worker.main(["/private/document.pdf"]) == 2
    assert b"/private/document.pdf" not in stderr.getvalue()
    assert stderr.getvalue().startswith(b"ERROR[BAD_PROTOCOL]:")
