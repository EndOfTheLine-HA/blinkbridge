import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_environment_login_overrides_blank_example(self):
        example = json.loads((ROOT / 'config' / 'config.example.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'config.json'
            config_path.write_text(json.dumps(example), encoding='utf-8')
            env = os.environ.copy()
            env.update({
                'BLINKBRIDGE_CONFIG': str(config_path),
                'BLINK_USERNAME': 'test-user',
                'BLINK_PASSWORD': 'test-password',  # pragma: allowlist secret
            })
            script = (
                'from blinkbridge.config import CONFIG; '
                'assert CONFIG["blink"]["login"]["username"] == "test-user"; '
                'assert CONFIG["blink"]["login"]["password"] == "test-password"'  # pragma: allowlist secret
            )
            result = subprocess.run(
                [sys.executable, '-c', script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
