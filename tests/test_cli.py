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
            # Point HOME inside the temp dir so the child never reads the
            # developer's real ~/.nexus/config.toml.
            env = {**os.environ, 'HOME': directory}
            result = subprocess.run(command, input='hello', capture_output=True, text=True, timeout=10, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            final = events[-1]
            # The event envelope gained additive fields (plan section 3.5);
            # type/data and event order are unchanged.
            self.assertEqual(final['type'], 'completed')
            self.assertEqual(final['data'], {'session': 'default', 'text': 'ok'})
            self.assertEqual(events[0]['type'], 'started')
            self.assertEqual(final['seq'], 0)
            self.assertIsNone(final['session'])
            self.assertIsNone(final['turn'])
            self.assertIsInstance(final['ts'], float)
            self.assertTrue(final['id'])
            (root / 'nexus.toml').write_text('unknown = 1')
            result = subprocess.run(command, input='hello', capture_output=True, text=True, timeout=10, env=env)
            self.assertEqual(result.returncode, 1)
            error = json.loads(result.stdout)
            self.assertEqual(error['type'], 'error')
            self.assertIn('Unknown', error['data']['message'])
            # CLI JSON errors now use the widened Event envelope.
            self.assertEqual(error['seq'], 0)
            self.assertTrue(error['id'])
            self.assertIsInstance(error['ts'], float)
