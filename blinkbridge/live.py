import asyncio
import logging
import re
import ssl
import types
from datetime import datetime

from blinkbridge.config import COMMON_FFMPEG_ARGS, CONFIG, RTSP_URL
from blinkbridge.utils import sanitize_stream_name

log = logging.getLogger(__name__)

# start code plus the header byte of an H.264 SPS NAL unit (nal_ref_idc 1 or 3)
SPS_START = re.compile(b'\x00\x00\x01[\x27\x67]')
# TS packets kept before an SPS, so ffmpeg also gets the start of the PES packet that carries it
PARAMS_LEAD = 8 * 188
# ffmpeg consumes its input continuously; if it stops reading for this long while still alive, the
# publisher is wedged rather than slow
WRITE_TIMEOUT = 10
# Blink's own live view sessions run about 361 s, so a far shorter one means the camera or the relay
# is struggling even when it did deliver some video
MIN_GOOD_SESSION_SECONDS = 120

async def recv_all_video(self):
    '''
    Replacement for blinkpy BlinkLiveStream.recv(). blinkpy only forwards type-0 payloads that
    start with the MPEG-TS sync byte 0x47 and drops the rest. With a Blink Mini live view that
    left an H.264 stream without SPS/PPS and keyframes (ffmpeg read 25 MB and never found the
    codec parameters). Here every type-0 payload is forwarded as it came and ffmpeg's MPEG-TS
    demuxer re-syncs by itself, like the blink-liveview-middleware reference does. Message type
    counts are logged per session.
    '''
    stats = {}
    try:
        while not self.target_reader.at_eof():
            header = await self._read_exactly(9)
            msgtype = header[0]
            payload_length = int.from_bytes(header[5:9], byteorder='big')
            if payload_length <= 0:
                continue
            payload = await self._read_exactly(payload_length)
            count = stats.setdefault(msgtype, [0, 0, 0])
            count[0] += 1
            count[1] += payload_length
            count[2] += payload[0] == 0x47
            if msgtype != 0x00:
                continue
            for writer in self.clients:
                if not writer.is_closing():
                    writer.write(payload)
                    await writer.drain()
            await asyncio.sleep(0)
    except (asyncio.IncompleteReadError, ssl.SSLError, ConnectionError) as e:
        log.debug(f"{self.camera.name}: live view connection ended: {e!r}")
    except Exception:
        log.exception(f"{self.camera.name}: error while receiving live view data")
    finally:
        summary = ', '.join(f"type {t}: {c[0]} msgs {c[1] / 1e6:.1f} MB ({c[2]} start with 0x47)" for t, c in sorted(stats.items()))
        log.info(f"{self.camera.name}: live view messages: {summary or 'none'}")
        self.target_writer.close()
        # end the local connection now: blinkpy only closes it after its command polling and the
        # "done" call to Blink, which often times out after about 12 s and delayed the next session
        for writer in self.clients:
            if not writer.is_closing():
                writer.close()


class LiveStreamServer:
    '''
    Publishes a camera's Blink live view to the RTSP server, instead of its clips and stills
    (cameras listed in cameras.live). Blink ends every live view after a while, so sessions are
    started one after another and fed into one long-running ffmpeg that stamps the video with
    wall-clock time: readers see one continuous stream with a short gap at each renewal, and no
    timestamp jumps. After cameras.live_max_failures failed sessions in a row it gives up and
    main.py switches the camera back to clips for cameras.live_fallback_minutes.
    '''
    is_live = True

    def __init__(self, stream_name: str, cam_manager):
        self.stream_name = stream_name
        self.stream_name_sanitized = sanitize_stream_name(stream_name)
        self.cam_manager = cam_manager
        self.ffmpeg = None
        self.task = None
        self.gave_up = False
        self.cleanups = set()
        self.params = None
        self.tail = b''
        self.max_failures = CONFIG['cameras'].get('live_max_failures', 5)
        # cameras.live_handover: cameras whose next session is opened before Blink ends the current one.
        # live_handover_after may be a single number or a per-camera mapping.  Per-camera
        # values let multiple live cameras renew at different times instead of hitting the
        # Blink cloud API in one burst.
        handover_after = CONFIG['cameras'].get('live_handover_after', 330)
        if stream_name in CONFIG['cameras'].get('live_handover', []):
            self.handover_after = (handover_after.get(stream_name)
                                   if isinstance(handover_after, dict) else handover_after)
        else:
            self.handover_after = None

    def start_server(self, file_name_initial_video=None) -> None:
        self.task = asyncio.get_running_loop().create_task(self._run())
        log.info(f"{self.stream_name}: live view stream ready at {RTSP_URL}/{self.stream_name_sanitized}")

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    def close(self) -> None:
        if self.task and not self.task.done():
            log.info(f"{self.stream_name}: stopping live view")
            self.task.cancel()
        for task in list(self.cleanups):
            task.cancel()
        self._stop_ffmpeg()

    def add_video(self, *args, **kwargs) -> None:
        # clips are not played while the camera is live
        pass

    async def _start_ffmpeg(self) -> None:
        ffmpeg_args = [
            'ffmpeg',
            *COMMON_FFMPEG_ARGS,
            # sessions restart the camera's own timestamps, wall-clock time keeps them continuous
            '-use_wallclock_as_timestamps', '1',
            # start publishing within a few seconds instead of analysing up to 5 MB of input first
            '-analyzeduration', '1000000',
            '-probesize', '500000',
            '-f', 'mpegts',
            '-i', 'pipe:0',
            '-map', '0:v',
            # video only: the Blink Mini live view declares two AAC tracks that carry no audio (0
            # channels); mapping them makes ffmpeg fail to set up the audio encoder and stall
            '-an',
            '-c:v', 'copy',
            '-f', 'rtsp',
            '-rtsp_transport', 'tcp',
            f"{RTSP_URL}/{self.stream_name_sanitized}",
        ]
        self.ffmpeg = await asyncio.create_subprocess_exec(*ffmpeg_args, stdin=asyncio.subprocess.PIPE)
        log.debug(f"{self.stream_name}: started ffmpeg pid {self.ffmpeg.pid}")
        if self.params:
            # an ffmpeg started in the middle of a session needs the parameter sets first
            self.ffmpeg.stdin.write(self.params)

    def _remember_params(self, chunk: bytes) -> None:
        '''
        Keep the stream data from just before the newest SPS to the end of its chunk. Blink sends
        the H.264 parameter sets (SPS/PPS) far less often than keyframes, and an ffmpeg started in
        the middle of a session (after the RTSP server dropped the publisher) cannot publish until
        it has seen them: it failed with "dimensions not set" for up to 30 s.
        '''
        data = self.tail + chunk
        found = None
        for found in SPS_START.finditer(data):
            pass
        if found is not None:
            self.params = data[max(0, found.start() - PARAMS_LEAD):]
        self.tail = chunk[-PARAMS_LEAD:]

    def _stop_ffmpeg(self) -> None:
        if self.ffmpeg and self.ffmpeg.returncode is None:
            try:
                self.ffmpeg.kill()
            except ProcessLookupError:
                pass
        self.ffmpeg = None

    async def _write(self, chunk: bytes) -> None:
        if self.ffmpeg is None or self.ffmpeg.returncode is not None:
            await self._start_ffmpeg()
        try:
            self.ffmpeg.stdin.write(chunk)
            # drain() has no deadline of its own. If ffmpeg stays alive but stops reading
            # stdin, for example because its RTSP output stalled, this await never returns: the
            # live task stays pending, is_running() keeps reporting healthy, and neither the
            # per-camera fallback nor the container restart can notice.
            await asyncio.wait_for(self.ffmpeg.stdin.drain(), timeout=WRITE_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning(f"{self.stream_name}: ffmpeg stopped reading its input for {WRITE_TIMEOUT} s, restarting it")
            self._stop_ffmpeg()
        except (BrokenPipeError, ConnectionResetError) as e:
            # ffmpeg exited (for example the RTSP server dropped it after a gap), start a new one
            log.debug(f"{self.stream_name}: ffmpeg input closed ({e}), restarting it")
            self._stop_ffmpeg()

    async def _open(self):
        '''
        Start a Blink live view session and connect to blinkpy's local relay of it.
        '''
        camera = self.cam_manager.blink.cameras[self.stream_name]
        stream = await camera.init_livestream()
        stream.recv = types.MethodType(recv_all_video, stream)
        await stream.start()
        s = types.SimpleNamespace(stream=stream, feed=None, reader=None, writer=None, started=datetime.now(), received=0)
        try:
            s.feed = asyncio.get_running_loop().create_task(stream.feed())
            host, port = stream.socket.getsockname()[:2]
            s.reader, s.writer = await asyncio.open_connection(host, port)
        except BaseException:
            self._close(s)
            raise
        return s

    def _close(self, s) -> None:
        if s.writer:
            s.writer.close()
        s.stream.stop()
        if s.feed:
            # blinkpy still has to finish polling and tell Blink the live view is done; that runs
            # in the background so the next session starts right away
            cleanup = asyncio.get_running_loop().create_task(self._finish_feed(s.feed))
            self.cleanups.add(cleanup)
            cleanup.add_done_callback(self.cleanups.discard)

    async def _open_next(self) -> tuple:
        '''
        Open the next session while the current one still runs. Returns it and its first chunk
        once it delivers video.
        '''
        s = await self._open()
        try:
            chunk = await asyncio.wait_for(s.reader.read(65536), timeout=20)
            if not chunk:
                raise ConnectionError('it ended without video')
        except BaseException:
            self._close(s)
            raise
        return s, chunk

    async def _feed(self, s, chunk: bytes) -> None:
        s.received += len(chunk)
        await self._write(chunk)
        self._remember_params(chunk)

    async def _session(self) -> tuple:
        '''
        Run Blink live view into ffmpeg until a session ends without a handover. Returns (seconds,
        bytes received) of that last session.
        For cameras in cameras.live_handover the next session is opened
        cameras.live_handover_after seconds into the current one (Blink ends them after about
        6 min), and the stream switches to it as soon as it delivers video. Renewing only after
        Blink had ended a session sometimes left 1-2 min without video: Blink answered with
        empty sessions until then.
        '''
        s = await self._open()
        handover = None
        tried = False
        last_video = datetime.now()
        try:
            while True:
                if handover is not None and handover.done():
                    try:
                        new, chunk = handover.result()
                    except Exception as e:
                        log.info(f"{self.stream_name}: next live view session not ready ({e!r}), keeping the current one")
                        handover = None
                    else:
                        seconds = (datetime.now() - s.started).total_seconds()
                        log.info(f"{self.stream_name}: handed over to the next live view session after {seconds:.0f} s ({s.received / 1e6:.1f} MB)")
                        self._close(s)
                        s, handover, tried = new, None, False
                        await self._feed(s, chunk)
                        last_video = datetime.now()
                        continue
                if (self.handover_after and not tried
                        and (datetime.now() - s.started).total_seconds() >= self.handover_after):
                    tried = True
                    handover = asyncio.get_running_loop().create_task(self._open_next())
                try:
                    chunk = await asyncio.wait_for(s.reader.read(65536), timeout=1)
                except asyncio.TimeoutError:
                    if (datetime.now() - last_video).total_seconds() >= 15:
                        # no video for 15 s means the session is stuck, end it and start a new one
                        log.warning(f"{self.stream_name}: no live video for 15 s, ending the session")
                        break
                    continue
                if not chunk:
                    if handover is not None:
                        # Blink ended this session while the next one is being opened: wait for it
                        try:
                            await asyncio.wait_for(asyncio.shield(handover), timeout=25)
                        except Exception:
                            pass
                        if handover.done():
                            continue
                    break
                await self._feed(s, chunk)
                last_video = datetime.now()
        finally:
            self._close(s)
            if handover is not None:
                handover.cancel()
        return (datetime.now() - s.started).total_seconds(), s.received

    async def _finish_feed(self, feed) -> None:
        try:
            await asyncio.wait_for(feed, timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            log.debug(f"{self.stream_name}: live view feed ended with {e}")

    async def _run(self) -> None:
        failures = 0
        try:
            while True:
                try:
                    seconds, received = await self._session()
                    log.info(f"{self.stream_name}: live view session ended after {seconds:.0f} s ({received / 1e6:.1f} MB)")
                    ok = received >= 50_000
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning(f"{self.stream_name}: live view failed: {e}")
                    ok, seconds = False, 0.0

                if not ok:
                    failures += 1
                elif seconds < MIN_GOOD_SESSION_SECONDS:
                    # Judging a session by payload alone meant a 20 s burst counted as a full
                    # success and reset the counter, so a camera that only ever managed short
                    # sessions never reached the fallback. Half a failure: five empty sessions
                    # still trip at max_failures, and so do ten short ones.
                    failures += 0.5
                    log.info(f"{self.stream_name}: live view session was short ({seconds:.0f} s), "
                             f"counting it as half a failure ({failures:g}/{self.max_failures})")
                else:
                    failures = 0
                if failures >= self.max_failures:
                    log.warning(f"{self.stream_name}: live view failed {failures:g} times in a row, falling back to clips")
                    self.gave_up = True
                    return

                # renew quickly so the RTSP server does not drop the publisher, back off on failures.
                # Blink sometimes rejects the first renewal after a good session and accepts the next
                # one, so the first retry comes after 2 s; later retries back off as before
                if failures == 0:
                    delay = 1
                elif failures <= 1:
                    delay = 2
                else:
                    delay = min(5 * 2 ** int(failures), 120)
                await asyncio.sleep(delay)
        finally:
            self._stop_ffmpeg()
