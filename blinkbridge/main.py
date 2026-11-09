import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta

from rich.highlighter import NullHighlighter
from rich.logging import RichHandler

from blinkbridge.blink import CameraManager
from blinkbridge.config import CONFIG, DELAY_RESTART
from blinkbridge.live import LiveStreamServer
from blinkbridge.stream_server import StreamServer
from blinkbridge.utils import RedactingFilter

log = logging.getLogger(__name__)

class Application:
    def __init__(self):
        self.stream_servers = {}
        self.cam_manager = None
        self.speaker_task = None
        self.running = False
        self.pending_cameras = set()
        self.datetime_pending_retry = datetime.now()
        self.refresh_requested = {}
        self.live_blocked_until = {}

    def use_live(self, camera_name: str) -> bool:
        # cameras.live lists the cameras streamed from Blink live view instead of clips
        return (camera_name in CONFIG['cameras'].get('live', [])
                and datetime.now() >= self.live_blocked_until.get(camera_name, datetime.min))

    async def start_stream(self, camera_name: str, redownload: bool=False) -> StreamServer:
        if self.use_live(camera_name):
            log.info(f"{camera_name}: starting live view stream server")
            stream_server = LiveStreamServer(camera_name, self.cam_manager)
            stream_server.start_server()
            self.stream_servers[camera_name] = stream_server
            return stream_server

        if redownload:
            await self.cam_manager.refresh_metadata()

        log.debug(f"{camera_name}: getting latest clip")
        # always fetch the newest clip: after a reboot the file on disk can be much older
        file_name_initial_video = await self.cam_manager.save_latest_clip(camera_name, force=True)

        log.info(f"{camera_name}: starting stream server")
        stream_server = StreamServer(camera_name)
        # building the initial still runs ffprobe/ffmpeg; off the loop so live cameras keep going
        await asyncio.to_thread(stream_server.start_server, file_name_initial_video)
        self.stream_servers[camera_name] = stream_server

        return stream_server

    async def check_for_motion(self, camera_name: str) -> bool:
        ss = self.stream_servers[camera_name]

        if not ss.is_running():
            return False
        if getattr(ss, 'is_live', False):
            # the camera is streaming its live view, clips are not played
            return False


        try:
            file_name_new_clip = await self.cam_manager.check_for_motion(camera_name)
        except Exception as e:
            # A Blink API or network error says nothing about the stream, which keeps looping its
            # current still. Closing the stream here turned every short outage into a failure.
            log.warning(f"{camera_name}: could not check Blink for motion, keeping current stream: {e}")
            return False

        if not file_name_new_clip:
            return False

        log.info(f"{ss.stream_name}: motion detected, adding video")
        # blocking work (waiting for ffmpeg, building the still): run it off the event loop, which
        # it used to stall for up to 210 s while every Blink camera stopped
        await asyncio.to_thread(ss.add_video, file_name_new_clip)

        return True

    async def start(self) -> None:
        self.running = True
        self.cam_manager = CameraManager()
        await self.cam_manager.start()

        # get enabled cameras
        enabled_cameras = set(CONFIG['cameras']['enabled']) if CONFIG['cameras']['enabled'] else set(self.cam_manager.get_cameras())
        enabled_cameras = enabled_cameras - set(CONFIG['cameras']['disabled'])
        log.info(f"enabled cameras: {enabled_cameras}")

        # create stream servers for each camera
        for camera in self.cam_manager.get_cameras():
            if camera not in enabled_cameras:
                continue

            try:
                ss = await self.start_stream(camera)
            except Exception as e:
                # usually a camera that has no clip yet, retried every cameras.pending_retry_seconds
                log.error(f"{camera}: could not start stream, will retry: {e}")
                self.pending_cameras.add(camera)
                continue
            ss.failure_count = 0
            ss.datetime_started = datetime.now()

        if mqtt_host := os.getenv("BLINKBRIDGE_MQTT_HOST"):
            from blinkbridge.speaker_mqtt import run

            self.speaker_task = asyncio.create_task(run(self, mqtt_host))

        log.info("monitoring cameras for motion")
        while self.running:
            # refresh Blink once per poll. It used to run inside every camera's motion check, and
            # after a failed refresh each camera retried it and failed again.
            try:
                await self.cam_manager.refresh()
                blink_ok = True
            except Exception as e:
                log.warning(f"Blink refresh failed, skipping motion checks this poll: {e}")
                blink_ok = False

            # check for motion on each stream server
            for camera_name in (self.stream_servers if blink_ok else []):
                try:
                    await self.check_for_motion(camera_name)
                except Exception as e:
                    log.error(f"{camera_name}: error checking for motion: {e}")
                    self.stream_servers[camera_name].close()

            # check if any stream servers are stopped and restart them
            for camera_name in list(self.stream_servers.keys()):
                ss = self.stream_servers[camera_name]

                if ss.is_running():
                    ss.datetime_last_running = datetime.now()
                    continue

                if not ss.is_running():
                    if getattr(ss, 'gave_up', False):
                        # live view kept failing: use clips for a while, then try live view again
                        minutes = CONFIG['cameras'].get('live_fallback_minutes', 30)
                        self.live_blocked_until[camera_name] = datetime.now() + timedelta(minutes=minutes)
                        log.warning(f"{camera_name}: using clips for {minutes} min, then trying live view again")
                        ss.failure_count = 0
                        ss.datetime_started = datetime.min  # restart now, not after the restart delay

                    # A server that had been up for a while before it died is not part of a crash loop.
                    # failure_count is copied into every restarted server and used to only ever grow, so
                    # three occasional deaths over the whole life of the process disabled a camera for good.
                    # Count from zero again after a stable stretch (cameras.stable_seconds, default 600).
                    up_seconds = (getattr(ss, 'datetime_last_running', ss.datetime_started) - ss.datetime_started).total_seconds()
                    if ss.failure_count and up_seconds >= CONFIG['cameras'].get('stable_seconds', 600):
                        log.info(f"{camera_name}: server had been up {up_seconds:.0f}s, resetting its failure count")
                        ss.failure_count = 0

                    # remove stream if too many failures
                    if ss.failure_count >= CONFIG['cameras']['max_failures'] - 1:
                        log.warning(f"{camera_name}: too many failures, disabling")
                        self.stream_servers.pop(camera_name)
                        continue

                    log.warning(f"{camera_name}: server failed {ss.failure_count + 1} time(s)")

                    # do nothing if stream was last started less certain time ago
                    if datetime.now() < ss.datetime_started + DELAY_RESTART:
                        continue

                    # create new stream server
                    try:
                        ss_new = await self.start_stream(camera_name, redownload=True)
                    except Exception as e:
                        # count it as another failure and retry after the restart delay; an exception
                        # escaping here used to end the monitoring loop while the process kept running
                        log.error(f"{camera_name}: could not restart stream: {e}")
                        ss.failure_count += 1
                        ss.datetime_started = datetime.now()
                        continue
                    ss_new.failure_count = ss.failure_count + 1
                    ss_new.datetime_started = datetime.now()

            # retry cameras that could not start, for example because no clip existed yet
            if self.pending_cameras and datetime.now() >= self.datetime_pending_retry:
                self.datetime_pending_retry = datetime.now() + timedelta(seconds=CONFIG['cameras'].get('pending_retry_seconds', 300))
                for camera_name in list(self.pending_cameras):
                    try:
                        ss = await self.start_stream(camera_name, redownload=True)
                    except Exception as e:
                        log.info(f"{camera_name}: stream still cannot start: {e}")
                        continue
                    ss.failure_count = 0
                    ss.datetime_started = datetime.now()
                    self.pending_cameras.discard(camera_name)

            # a live camera that fell back to clips goes back to live view once its wait is over
            for camera_name, ss in list(self.stream_servers.items()):
                if (camera_name in self.live_blocked_until and self.use_live(camera_name)
                        and not getattr(ss, 'is_live', False)):
                    log.info(f"{camera_name}: trying live view again")
                    del self.live_blocked_until[camera_name]
                    ss.close()
                    try:
                        ss_new = await self.start_stream(camera_name)
                        ss_new.failure_count = 0
                        ss_new.datetime_started = datetime.now()
                    except Exception as e:
                        log.error(f"{camera_name}: could not start live view: {e}")

            # optional: ask Blink for a new clip from a camera that has had none for a while
            if blink_ok:
                await self.request_refresh_clip()

            await asyncio.sleep(CONFIG['blink']['poll_interval'])

    async def request_refresh_clip(self) -> None:
        '''
        BlinkBridge only shows new clips, so a quiet camera keeps showing an old picture. With
        blink.refresh_minutes set, a camera whose last clip is older than that is asked to record
        one. At most one camera per poll, so record commands are spread out, and a camera with
        real motion recently is left alone. blink.refresh_cameras limits it to some cameras.
        '''
        refresh_s = CONFIG['blink'].get('refresh_minutes', 0) * 60
        if refresh_s <= 0:
            return
        wanted = set(CONFIG['blink'].get('refresh_cameras') or self.stream_servers)
        now = datetime.now()
        for camera_name, ss in self.stream_servers.items():
            if camera_name not in wanted or not ss.is_running() or getattr(ss, 'is_live', False):
                continue
            since_clip = self.cam_manager.seconds_since_last_clip(camera_name)
            requested = self.refresh_requested.get(camera_name)
            if since_clip is not None and since_clip < refresh_s:
                continue
            if requested is not None and (now - requested).total_seconds() < refresh_s:
                continue
            self.refresh_requested[camera_name] = now
            age = f"for {since_clip / 60:.0f} min" if since_clip is not None else "yet"
            try:
                await self.cam_manager.request_clip(camera_name)
                log.info(f"{camera_name}: no clip {age}, asked Blink to record one")
            except Exception as e:
                log.warning(f"{camera_name}: could not ask Blink to record a clip: {e}")
            return

    async def close(self) -> None:
        self.running = False

        if self.speaker_task:
            self.speaker_task.cancel()
            await asyncio.gather(self.speaker_task, return_exceptions=True)

        if self.cam_manager:
            await self.cam_manager.close()

        for ss in self.stream_servers.values():
            ss.close()

async def main() -> int:
    app = Application()
    exit_code = 0

    # Create a cancellation event to coordinate shutdown
    shutdown_event = asyncio.Event()

    def handle_exit():
        # Signal the shutdown event when Ctrl+C is received
        shutdown_event.set()

    # Add signal handlers using loop.add_signal_handler
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_exit)

    try:
        # Start the application
        start_task = asyncio.create_task(app.start())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        # Wait for a shutdown signal, or for the monitoring loop to end by itself. Before, an
        # exception in app.start() was never looked at: the process stayed up doing nothing and
        # the ffmpeg processes kept looping their last still, so nothing noticed.
        await asyncio.wait({start_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)

        if start_task.done() and not start_task.cancelled() and start_task.exception():
            error = start_task.exception()
            log.error(f"Monitoring stopped unexpectedly, exiting so the container restarts: "
                      f"{type(error).__name__}: {error}")
            exit_code = 1
            # wait before exiting so that a lasting problem (Blink down, login failing) does not
            # become a tight loop of container restarts and logins; a stop signal ends the wait
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=CONFIG['blink'].get('restart_after_error_seconds', 120))
            except asyncio.TimeoutError:
                pass
        else:
            log.info("Shutting down...")

        # Cancel the tasks and wait for them to complete
        shutdown_task.cancel()
        start_task.cancel()
        try:
            await start_task
        except (asyncio.CancelledError, Exception):
            pass

    except Exception as e:
        log.error(f"Unexpected error: {e}")
        exit_code = 1

    finally:
        # Ensure app is closed gracefully
        await app.close()

    return exit_code

if __name__ == "__main__":
    handler = RichHandler(highlighter=NullHighlighter())
    handler.addFilter(RedactingFilter())
    logging.basicConfig(
        format="%(message)s", datefmt="[%X]", handlers=[handler]
    )
    logging.getLogger('blinkbridge').setLevel(CONFIG['log_level'])
    logging.getLogger(__name__).setLevel(CONFIG['log_level'])

    sys.exit(asyncio.run(main()))
