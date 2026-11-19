import asyncio
import atexit
import json
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

if 'BLINKBRIDGE_CONFIG' not in os.environ:
    # importing blinkbridge.live loads the config, so give it the example one
    ROOT = Path(__file__).resolve().parents[1]
    TEMP_DIRECTORY = tempfile.TemporaryDirectory()
    atexit.register(TEMP_DIRECTORY.cleanup)
    config = json.loads((ROOT / 'config' / 'config.example.json').read_text(encoding='utf-8'))
    config['blink']['login'] = {'username': 'test-user', 'password': 'test-password'}  # pragma: allowlist secret
    config['paths'] = {key: TEMP_DIRECTORY.name for key in ('videos', 'concat', 'config')}
    config_path = Path(TEMP_DIRECTORY.name) / 'config.json'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    os.environ['BLINKBRIDGE_CONFIG'] = str(config_path)

from blinkbridge import live

START = datetime(2026, 1, 1, 12, 0, 0)


class FakeClock:
    '''Stands in for live.datetime: every read from a session advances time by 10 s.'''

    def __init__(self):
        self.value = START

    def now(self):
        return self.value


class FakeReader:
    def __init__(self, clock, chunks):
        self.clock = clock
        self.chunks = list(chunks)

    async def read(self, n):
        await asyncio.sleep(0)
        self.clock.value += timedelta(seconds=10)
        return self.chunks.pop(0) if self.chunks else b''


def make_server(clock, sessions, handover_after=300):
    server = object.__new__(live.LiveStreamServer)
    server.stream_name = 'Test Cam'
    server.speaker_writer = None
    server.handover_after = handover_after
    pending = list(sessions)

    def new_session(chunks):
        return types.SimpleNamespace(reader=FakeReader(clock, chunks), started=clock.now(), received=0)

    async def fake_open():
        return new_session(pending.pop(0))

    async def fake_open_next():
        s = new_session(pending.pop(0))
        return s, await s.reader.read(65536)

    async def fake_feed(s, chunk):
        s.received += len(chunk)

    server._open = fake_open
    server._open_next = fake_open_next
    server._close = lambda s: None
    server._feed = fake_feed
    return server


class SessionChainTests(unittest.TestCase):
    def run_session(self, sessions, handover_after=300):
        clock = FakeClock()
        server = make_server(clock, sessions, handover_after)
        with mock.patch.object(live, 'datetime', clock):
            return asyncio.run(server._session())

    def test_handover_chain_is_timed_from_the_first_session(self):
        # Blink ends sessions after about 361 s; the handover happens at 300 s, so the last
        # session alone is only about 60 s long. The chain as a whole must not look short.
        first = [b'x' * 1000] * 40
        second = [b'y' * 1000] * 7
        seconds, received, sessions = self.run_session([first, second])
        self.assertEqual(sessions, 2)
        self.assertGreaterEqual(seconds, live.MIN_GOOD_SESSION_SECONDS)
        self.assertGreater(seconds, 300)
        # bytes from both sessions count: at least the first 300 s plus all of the second
        self.assertGreaterEqual(received, 30 * 1000 + 7 * 1000)

    def test_single_short_session_still_reports_short(self):
        seconds, received, sessions = self.run_session([[b'z' * 1000] * 3])
        self.assertEqual(sessions, 1)
        self.assertLess(seconds, live.MIN_GOOD_SESSION_SECONDS)
        self.assertEqual(received, 3000)

    def test_without_handover_one_session_is_reported(self):
        seconds, received, sessions = self.run_session([[b'a' * 10] * 36], handover_after=None)
        self.assertEqual(sessions, 1)
        self.assertEqual(received, 360)


if __name__ == '__main__':
    unittest.main()
