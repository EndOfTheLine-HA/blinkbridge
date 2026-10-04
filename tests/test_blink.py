import atexit
import importlib
import json
import os
import stat
import tempfile
import unittest
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP_DIRECTORY = tempfile.TemporaryDirectory()
atexit.register(TEMP_DIRECTORY.cleanup)
temporary_path = Path(TEMP_DIRECTORY.name)
config = json.loads((ROOT / 'config' / 'config.example.json').read_text(encoding='utf-8'))
config['blink']['login'] = {'username': 'test-user', 'password': 'test-password'}  # pragma: allowlist secret
config['paths'] = {
    'videos': str(temporary_path),
    'concat': str(temporary_path),
    'config': str(temporary_path),
}
config_path = temporary_path / 'config.json'
config_path.write_text(json.dumps(config), encoding='utf-8')
os.environ['BLINKBRIDGE_CONFIG'] = str(config_path)

from blinkbridge.blink import is_playable_clip, parse_blink_time, write_file_atomic


class BlinkHelpersTests(unittest.TestCase):
    def test_runtime_modules_import(self):
        for module in ('blinkbridge.ffmpeg', 'blinkbridge.live', 'blinkbridge.main', 'blinkbridge.stream_server'):
            self.assertIsNotNone(importlib.import_module(module))

    def test_parse_blink_time_is_timezone_aware(self):
        parsed = parse_blink_time('2026-01-02T03:04:05Z')
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_playable_clip_rejects_snapshot_and_partial(self):
        self.assertTrue(is_playable_clip({'media': '/media/video.mp4'}))
        self.assertFalse(is_playable_clip({'media': '/media/image.jpg', 'source': 'snapshot'}))
        self.assertFalse(is_playable_clip({'media': '/media/video.mp4', 'partial': True}))

    def test_atomic_write_replaces_content(self):
        destination = temporary_path / 'credential-test.json'
        write_file_atomic(destination, b'first', mode=0o600)
        write_file_atomic(destination, b'second', mode=0o600)
        self.assertEqual(destination.read_bytes(), b'second')
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
