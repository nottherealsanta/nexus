import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from nexus.cli import initialize


class CliTests(unittest.TestCase):
    def test_init_preserves_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'SOUL.md').write_text('custom')
            initialize(root)
            self.assertEqual((root / 'SOUL.md').read_text(), 'custom')
            self.assertTrue((root / 'nexus.toml').exists())

    def test_json_cli_success_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'codex'
            fake.write_text(f'#!{sys.executable}\nimport json\nprint(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":"ok"}}}}))\nprint(json.dumps({{"type":"turn.completed"}}))\n')
            fake.chmod(0o755)
            (root / 'nexus.toml').write_text(f'executable = {json.dumps(str(fake))}\n')
            command = [sys.executable, '-m', 'nexus', '--workspace', str(root), 'run', '-', '--json']
            result = subprocess.run(command, input='hello', capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(events[-1], {'type': 'completed', 'data': {'session': 'default', 'text': 'ok'}})
            (root / 'nexus.toml').write_text('unknown = 1')
            result = subprocess.run(command, input='hello', capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)['type'], 'error')
