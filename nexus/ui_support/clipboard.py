"""Read user-requested local clipboard images for attachment upload (PLAN §14.4)."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

MAX_IMAGE_BYTES = 8 * 1024 * 1024

_MAC_IMAGE = r"""
ObjC.import('AppKit');
function run(args) {
    const board = $.NSPasteboard.generalPasteboard;
    let data = board.dataForType($.NSPasteboardTypePNG);
    if (!data) {
        const tiff = board.dataForType($.NSPasteboardTypeTIFF);
        if (!tiff) return;
        const bitmap = $.NSBitmapImageRep.imageRepWithData(tiff);
        data = bitmap.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
    }
    if (!data) throw Error('Could not convert clipboard image');
    if (data.length > 8388608) throw Error('Clipboard image exceeds 8 MiB');
    if (!data.writeToFileAtomically($(args[0]), true)) throw Error('Could not read clipboard image');
}
"""


def read_clipboard_image() -> bytes | None:
    """Return PNG bytes, or None for non-image/unavailable local clipboards.

    Native commands are bounded by a five-second timeout. Never query another
    machine's clipboard when the terminal is connected through SSH.
    """
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY'):
        return None
    with tempfile.TemporaryDirectory(prefix='nexus-clipboard-') as directory:
        target = Path(directory) / 'clipboard.png'
        if sys.platform == 'darwin':
            try:
                result = subprocess.run(
                    ['osascript', '-l', 'JavaScript', '-e', _MAC_IMAGE, str(target)],
                    capture_output=True, timeout=5, check=False,
                )
            except subprocess.TimeoutExpired:
                raise ValueError("Clipboard image read timed out") from None
            if result.returncode:
                raise ValueError('Could not read clipboard image (conversion failed or image exceeds 8 MiB)')
        elif sys.platform.startswith('linux'):
            if os.environ.get('WAYLAND_DISPLAY') and shutil.which('wl-paste'):
                command = ['wl-paste', '--no-newline', '--type', 'image/png']
            elif os.environ.get('DISPLAY') and shutil.which('xclip'):
                command = ['xclip', '-selection', 'clipboard', '-t', 'image/png', '-o']
            else:
                return None
            # Stream through a pipe so oversized clipboard data is never retained.
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                timer = threading.Timer(5, process.kill)
                timer.start()
                try:
                    data = process.stdout.read(MAX_IMAGE_BYTES + 1)
                    if len(data) > MAX_IMAGE_BYTES:
                        process.kill()
                        raise ValueError('Clipboard image exceeds 8 MiB')
                    process.wait(timeout=1)
                    if process.returncode == -9:
                        raise ValueError('Clipboard image read timed out')
                finally:
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                if process.returncode:
                    return None
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError('Clipboard image exceeds 8 MiB')
                return data or None
        else:
            return None
        if not target.exists():
            return None
        with target.open('rb') as image:
            data = image.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError('Clipboard image exceeds 8 MiB')
        return data or None
