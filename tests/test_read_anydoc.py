from __future__ import annotations

import asyncio
import importlib.metadata
import os
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, PermissionsSection
from nexus.errors import OperationCancelled
from nexus.tools.builtin import _anydoc_client as anydoc_client
from nexus.tools.builtin import read
from nexus.tools.spec import ToolContext


def make_ctx(workspace: Path, cancel_token: object | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="s1",
        turn_id="t1",
        config=Config(
            v2=ConfigV2(
                permissions=PermissionsSection(
                    mode="allow", write_roots=["./"], read_denyroots=[]
                )
            )
        ),
        cancel_token=cancel_token,
    )


def make_ctx_with_denyroots(workspace: Path, *denyroots: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="s1",
        turn_id="t1",
        config=Config(
            v2=ConfigV2(
                permissions=PermissionsSection(
                    mode="allow",
                    write_roots=["./"],
                    read_denyroots=[str(path) for path in denyroots],
                )
            )
        ),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


async def test_document_markdown_offset_limit_and_source_metadata(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "report.pdf").write_bytes(b"pdf bytes")
    calls: list[tuple[bytes, str]] = []

    async def convert(payload: bytes, extension: str, cancel_token: object) -> bytes:
        calls.append((payload, extension))
        return b"title\nfirst\nsecond"

    monkeypatch.setattr(read, "convert_document", convert)
    result = await read.run(
        {"path": "report.pdf", "offset": 2, "limit": 2}, make_ctx(workspace)
    )

    assert result.is_error is False
    assert result.content[0].text == "first\nsecond"
    assert result.metrics["source"] == "firecrawl-anydoc"
    assert result.metrics["document"] is True
    assert result.metrics["extension"] == ".pdf"
    assert calls == [(b"pdf bytes", ".pdf")]
    assert "Firecrawl AnyDoc" in (result.display or "")


async def test_csv_is_raw_utf8_text_without_conversion(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "data.csv").write_text("name,value\nAda,1", encoding="utf-8")

    async def convert(*args: object) -> bytes:
        pytest.fail("CSV should not convert without csv_as_markdown")

    monkeypatch.setattr(read, "convert_document", convert)
    result = await read.run({"path": "data.csv"}, make_ctx(workspace))

    assert result.is_error is False
    assert result.content[0].text == "name,value\nAda,1"
    assert "source" not in result.metrics


async def test_csv_can_be_explicitly_converted_to_markdown(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "data.csv").write_bytes(b"name,value\nAda,1")
    calls: list[tuple[bytes, str]] = []

    async def convert(payload: bytes, extension: str, cancel_token: object) -> bytes:
        calls.append((payload, extension))
        return b"| name | value |\n| --- | --- |\n| Ada | 1 |"

    monkeypatch.setattr(read, "convert_document", convert)
    result = await read.run(
        {"path": "data.csv", "csv_as_markdown": True}, make_ctx(workspace)
    )

    assert result.is_error is False
    assert result.content[0].text.endswith("| Ada | 1 |")
    assert result.metrics["source"] == "firecrawl-anydoc"
    assert result.metrics["extension"] == ".csv"
    assert calls == [(b"name,value\nAda,1", ".csv")]


async def test_csv_conversion_option_is_rejected_for_non_csv(workspace: Path):
    (workspace / "notes.txt").write_text("plain", encoding="utf-8")
    result = await read.run(
        {"path": "notes.txt", "csv_as_markdown": True}, make_ctx(workspace)
    )
    assert result.is_error is True
    assert "only supported for .csv" in result.content[0].text


async def test_document_outside_workspace_is_allowed_when_guard_allows(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"external document")

    async def convert(payload: bytes, extension: str, cancel_token: object) -> bytes:
        assert payload == b"external document"
        assert extension == ".pdf"
        return b"converted external document"

    monkeypatch.setattr(read, "convert_document", convert)
    result = await read.run({"path": str(outside)}, make_ctx(workspace))

    assert result.is_error is False
    assert result.content[0].text == "converted external document"


async def test_document_outside_workspace_under_denyroot_is_blocked(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"private document")

    async def convert(*args: object) -> bytes:
        pytest.fail("a read-denyroot document must not be converted")

    monkeypatch.setattr(read, "convert_document", convert)
    result = await read.run(
        {"path": str(outside)}, make_ctx_with_denyroots(workspace, tmp_path)
    )

    assert result.is_error is True
    assert "read-deny" in result.content[0].text


async def test_document_result_byte_cap_is_explicit(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "report.csv").write_bytes(b"csv")
    monkeypatch.setattr(read, "_DEFAULT_MAX_BYTES", 8)

    async def convert(*args: object) -> bytes:
        return b"12345678901234567890"

    monkeypatch.setattr(read, "convert_document", convert)
    result = await read.run(
        {"path": "report.csv", "csv_as_markdown": True}, make_ctx(workspace)
    )

    assert result.is_error is False
    assert result.metrics["truncated"] is True
    assert "truncated" in result.content[0].text
    assert "re-run with offset=" in result.content[0].text


async def test_oversize_document_is_rejected_before_worker_spawn(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "large.pdf").write_bytes(b"x" * (16 * 1024 * 1024 + 1))

    async def spawn(*args: object) -> None:
        pytest.fail("oversize file must not spawn the worker")

    monkeypatch.setattr(read, "convert_document", spawn)
    result = await read.run({"path": "large.pdf"}, make_ctx(workspace))
    assert result.is_error is True
    assert "16 MiB" in result.content[0].text


async def test_document_worker_errors_are_content_free(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "bad.docx").write_bytes(b"private document payload")

    async def unavailable(*args: object) -> bytes:
        raise anydoc_client.AnyDocError(
            "Firecrawl AnyDoc is unavailable; install nexus-harness[documents]."
        )

    monkeypatch.setattr(read, "convert_document", unavailable)
    result = await read.run({"path": "bad.docx"}, make_ctx(workspace))
    shown = result.content[0].text
    assert result.is_error is True
    assert "nexus-harness[documents]" in shown
    assert "private document payload" not in shown


async def test_worker_stderr_error_code_classifies_missing_extra(
    monkeypatch: pytest.MonkeyPatch,
):
    class Process:
        returncode = 2

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            assert payload == b"document"
            return b"", b"ERROR[ANYDOC_UNAVAILABLE]: optional package missing\n"

    async def spawn(extension: str) -> Process:
        return Process()

    monkeypatch.setattr(anydoc_client, "_spawn_worker", spawn)
    with pytest.raises(anydoc_client.AnyDocError, match=r"nexus-harness\[documents\]"):
        await anydoc_client.convert_document(b"document", ".pdf", None)


async def test_spawn_worker_uses_minimal_environment_and_safe_cwd(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
):
    secret_names = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "FIRECRAWL_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NETRC",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PYTHONPATH",
    )
    for name in secret_names:
        monkeypatch.setenv(name, f"secret-{name}")
    captured: dict[str, object] = {}

    class Process:
        pass

    async def create_subprocess_exec(*args: object, **kwargs: object) -> Process:
        captured["args"] = args
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(
        anydoc_client.asyncio, "create_subprocess_exec", create_subprocess_exec
    )
    await anydoc_client._spawn_worker(".csv")

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PATH"] == os.defpath
    assert not (set(secret_names) & env.keys())
    assert set(env) <= {"PATH", "PYTHONSAFEPATH", "SystemRoot"}
    assert all(not value.startswith("secret-") for value in env.values())
    assert Path(captured["cwd"]).is_absolute()
    assert not Path(captured["cwd"]).is_relative_to(workspace)
    args = captured["args"]
    assert isinstance(args, tuple)
    assert "-I" in args
    assert str(Path(anydoc_client.__file__).resolve().with_name("_anydoc_worker.py")) in args
    assert captured["start_new_session"] is True


@pytest.mark.parametrize("launch_from_workspace", [False, True])
async def test_real_worker_cannot_be_shadowed_by_workspace_package(
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_from_workspace: bool,
):
    worker = workspace / "nexus" / "tools" / "builtin"
    worker.mkdir(parents=True)
    (worker / "_anydoc_worker.py").write_text(
        "import sys\nsys.stdout.buffer.write(b'WORKSPACE_SHADOW')\n"
    )
    monkeypatch.chdir(workspace if launch_from_workspace else Path(__file__).parents[1])

    proc = await anydoc_client._spawn_worker(".csv")
    stdout, stderr = await proc.communicate(b"name,value\nexample,1\n")

    assert b"WORKSPACE_SHADOW" not in stdout + stderr
    if proc.returncode == 0:
        assert stderr == b""
        assert stdout
    else:
        assert proc.returncode == 2
        assert stderr.startswith(b"ERROR[")


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "message"),
    [
        (b"x" * (anydoc_client.MAX_STDOUT_BYTES + 1), b"", 0, "output limit"),
        (b"", b"x" * (anydoc_client.MAX_STDERR_BYTES + 1), 2, "diagnostic limit"),
        (
            anydoc_client.SUCCESS_MAGIC + (1).to_bytes(4, "big") + b"\xff",
            b"",
            0,
            "non-UTF-8",
        ),
    ],
)
async def test_worker_output_is_bounded_and_utf8_validated(
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    message: str,
):
    class Process:
        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            return stdout, stderr

        def __init__(self) -> None:
            self.returncode = returncode

    async def spawn(extension: str) -> Process:
        return Process()

    monkeypatch.setattr(anydoc_client, "_spawn_worker", spawn)
    with pytest.raises(anydoc_client.AnyDocError, match=message):
        await anydoc_client.convert_document(b"document", ".pdf", None)


@pytest.mark.parametrize(
    "stdout",
    [
        b"noise" + anydoc_client.SUCCESS_MAGIC + (1).to_bytes(4, "big") + b"x",
        anydoc_client.SUCCESS_MAGIC + (4).to_bytes(4, "big") + b"x",
        anydoc_client.SUCCESS_MAGIC + (anydoc_client.MAX_MARKDOWN_BYTES + 1).to_bytes(4, "big"),
        anydoc_client.SUCCESS_MAGIC + (1).to_bytes(4, "big") + b"xtrailing",
        anydoc_client.SUCCESS_MAGIC,
    ],
)
async def test_client_rejects_garbage_truncated_and_oversized_frames(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes
):
    class Process:
        returncode = 0

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            return stdout, b""

    async def spawn(extension: str) -> Process:
        return Process()

    monkeypatch.setattr(anydoc_client, "_spawn_worker", spawn)
    with pytest.raises(anydoc_client.AnyDocError, match="protocol error"):
        await anydoc_client.convert_document(b"document", ".pdf", None)


async def test_worker_error_code_after_warnings_is_classified_without_leaking_them(
    monkeypatch: pytest.MonkeyPatch,
):
    class Process:
        returncode = 2

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            return b"", b"warning with private data\nERROR[ANYDOC_UNAVAILABLE]: missing\n"

    async def spawn(extension: str) -> Process:
        return Process()

    monkeypatch.setattr(anydoc_client, "_spawn_worker", spawn)
    with pytest.raises(anydoc_client.AnyDocError) as exc:
        await anydoc_client.convert_document(b"document", ".pdf", None)
    assert "nexus-harness[documents]" in str(exc.value)
    assert "private data" not in str(exc.value)


async def test_real_worker_converts_csv_to_a_complete_frame():
    try:
        importlib.metadata.version("firecrawl-anydoc")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("firecrawl-anydoc is not installed; install the documents extra")

    proc = await anydoc_client._spawn_worker(".csv")
    stdout, stderr = await proc.communicate(b"name,value\nAda,1\n")

    assert proc.returncode == 0, stderr.decode("ascii", errors="replace")
    assert stderr == b""
    assert anydoc_client._decode_success_frame(stdout)


async def test_unsupported_binary_still_fails_without_worker(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "archive.bin").write_bytes(b"\x00\xffbinary")

    async def spawn(*args: object) -> None:
        pytest.fail("unsupported binary must not spawn the worker")

    monkeypatch.setattr(read, "convert_document", spawn)
    result = await read.run({"path": "archive.bin"}, make_ctx(workspace))
    assert result.is_error is True
    assert result.metrics["binary"] is True


async def test_unsupported_non_utf8_binary_fails_without_replacement(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "archive.bin").write_bytes(b"\xff\xfe opaque")

    async def spawn(*args: object) -> bytes:
        pytest.fail("unsupported binary must not invoke AnyDoc")

    monkeypatch.setattr(read, "convert_document", spawn)
    result = await read.run({"path": "archive.bin"}, make_ctx(workspace))
    assert result.is_error is True
    assert "�" not in result.content[0].text


async def test_document_symlink_outside_workspace_is_allowed(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside document")
    (workspace / "escape.pdf").symlink_to(outside)

    async def convert(payload: bytes, extension: str, cancel_token: object) -> bytes:
        assert payload == b"outside document"
        assert extension == ".pdf"
        return b"converted outside document"

    monkeypatch.setattr(read, "convert_document", convert)
    result = await read.run({"path": "escape.pdf"}, make_ctx(workspace))
    assert result.is_error is False
    assert result.content[0].text == "converted outside document"


async def test_plain_text_path_is_unchanged(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    (workspace / "notes.txt").write_bytes(b"plain\r\ntext")

    async def spawn(*args: object) -> bytes:
        pytest.fail("plain text must not invoke AnyDoc")

    monkeypatch.setattr(read, "convert_document", spawn)
    result = await read.run({"path": "notes.txt"}, make_ctx(workspace))
    assert result.content[0].text == "plain\ntext"
    assert "source" not in result.metrics


class _BlockingProcess:
    pid = 12345
    returncode: int | None = None

    def __init__(self) -> None:
        self.killed = asyncio.Event()

    async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
        assert payload == b"private bytes"
        await self.killed.wait()
        self.returncode = -9
        return b"", b""


@pytest.mark.parametrize("abort", ["timeout", "cancel"])
async def test_client_kills_worker_group_on_timeout_or_task_cancellation(
    monkeypatch: pytest.MonkeyPatch, abort: str
):
    proc = _BlockingProcess()
    killed_pids: list[int] = []

    async def spawn(extension: str) -> _BlockingProcess:
        assert extension == ".pdf"
        return proc

    def killpg(pid: int, sig: int) -> None:
        assert sig
        killed_pids.append(pid)
        proc.killed.set()

    monkeypatch.setattr(anydoc_client, "_spawn_worker", spawn)
    monkeypatch.setattr(anydoc_client.os, "killpg", killpg)
    if abort == "timeout":
        monkeypatch.setattr(anydoc_client, "WORKER_TIMEOUT_SECONDS", 0.01)
        with pytest.raises(anydoc_client.AnyDocError, match="timed out"):
            await anydoc_client.convert_document(b"private bytes", ".pdf", None)
    else:
        task = asyncio.create_task(
            anydoc_client.convert_document(b"private bytes", ".pdf", None)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert killed_pids == [proc.pid]


async def test_client_converts_cancel_token_to_operation_cancelled(
    monkeypatch: pytest.MonkeyPatch,
):
    proc = _BlockingProcess()

    class CancelToken:
        cancelled = True

        async def wait(self) -> None:
            await asyncio.sleep(0)

        def raise_if_cancelled(self) -> None:
            raise OperationCancelled("cancelled")

    async def spawn(extension: str) -> _BlockingProcess:
        return proc

    def killpg(pid: int, sig: int) -> None:
        proc.killed.set()

    monkeypatch.setattr(anydoc_client, "_spawn_worker", spawn)
    monkeypatch.setattr(anydoc_client.os, "killpg", killpg)
    with pytest.raises(OperationCancelled):
        await anydoc_client.convert_document(b"private bytes", ".pdf", CancelToken())
