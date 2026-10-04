import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Union

from blinkbridge.config import COMMON_FFMPEG_ARGS, CONFIG, PATH_CONCAT, PATH_VIDEOS, RTSP_URL
from blinkbridge.ffmpeg import make_still_video
from blinkbridge.utils import sanitize_stream_name, wait_until_file_open

log = logging.getLogger(__name__)

class StreamServer:
    def __init__(self, stream_name: str):
        self.stream_name = stream_name
        self.stream_name_sanitized = sanitize_stream_name(stream_name)
        self.current_still_video = None

    def _run_server(self) -> str:
        output_url = f"{RTSP_URL}/{self.stream_name_sanitized}"
        input_concat_file = PATH_CONCAT / f"{self.stream_name_sanitized}.concat"

        ffmpeg_args = [
            'ffmpeg',
            *COMMON_FFMPEG_ARGS,
            '-fflags', '+igndts+genpts',
            '-re',
            '-stream_loop', '-1',
            '-f', 'concat',
            '-safe', '0',
            '-i', input_concat_file.resolve(),
            '-flush_packets', '0',
            '-c:v', 'copy',
            '-c:a', 'copy',
            '-f', 'rtsp',
            # TCP: over UDP MediaMTX logged hundreds of lost RTP packets per publisher session,
            # which reach every reader as broken frames
            '-rtsp_transport', 'tcp',
            # '-avoid_negative_ts', '1',
            # '-use_wallclock_as_timestamps', '1',
            '-fps_mode', 'drop',
            output_url
        ]

        self.process = subprocess.Popen(ffmpeg_args, stdout=sys.stdout, stderr=sys.stderr)

        return output_url

    def _make_concat_files(self) -> str:
        log.debug(f"{self.stream_name}: making concat file")

        next_concat = PATH_CONCAT / f"{self.stream_name_sanitized}_next.concat"
        concat_file = PATH_CONCAT / f"{self.stream_name_sanitized}.concat"

        with open(concat_file, 'w') as f:
            f.write("ffconcat version 1.0\n")
            f.write(f"file '{next_concat.resolve()}'\n")
            f.write("option safe 0\n") # needed to propogate 'safe 0' to next concat file
            f.write(f"file '{next_concat.resolve()}'\n")
            f.write("option safe 0\n") # needed to propogate 'safe 0' to next concat file

        return concat_file

    def _enqueue_clip(self, video_file_name: Union[str, Path]) -> Path:
        log.debug(f"{self.stream_name}: enqueueing {video_file_name}")

        video_file_name = Path(video_file_name)
        next_concat = PATH_CONCAT / f"{self.stream_name_sanitized}_next.concat"

        with open(next_concat, 'w') as f:
            f.write("ffconcat version 1.0\n")
            f.write(f"file '{video_file_name.resolve()}'\n")

        return next_concat

    def add_video(self, file_name_input_video: Union[str, Path], still_only: bool=False) -> None:
        # Blocking: it waits for ffmpeg to open the clip and runs ffprobe/ffmpeg to build the still.
        # Call it from a worker thread (asyncio.to_thread), never directly from a coroutine, or every
        # camera stalls while it runs.
        if file_name_input_video is None or not Path(file_name_input_video).exists():
            raise FileNotFoundError(f"{self.stream_name}: no clip to play ({file_name_input_video})")

        if not still_only:
            # enqueue fullclip immediately
            self._enqueue_clip(file_name_input_video)

        # make a timestamped name for the next still video
        dt = datetime.now()
        next_still_video = PATH_VIDEOS / f"{self.stream_name_sanitized}_still_{dt.strftime('%Y-%m-%d_%H-%M-%S-%f')}.mp4"

        # the still must not replace the clip in the playlist before ffmpeg has opened the clip
        if not still_only:
            log.debug(f"{self.stream_name}: waiting for new video to start")
            wait_until_file_open(file_name_input_video, self.process.pid)

        log.debug(f"{self.stream_name}: creating next still video {next_still_video}")
        try:
            make_still_video(file_name_input_video,
                             output_duration=CONFIG['still_video_duration'],
                             file_name_still_video=next_still_video)
        except Exception as e:
            # a failed build can leave a partial file behind; it would never be played or deleted
            next_still_video.unlink(missing_ok=True)
            if still_only or self.current_still_video is None:
                raise
            # keep looping the last good still instead of pointing the playlist at a missing file
            log.error(f"{self.stream_name}: could not make a still from the new clip, keeping the previous still: {e}")
            self._enqueue_clip(self.current_still_video)
            return
        self._enqueue_clip(next_still_video)

        # delete old still video
        if self.current_still_video and not still_only:
            log.debug(f'{self.stream_name}: deleting old still video {self.current_still_video}')
            self.current_still_video.unlink()

        self.current_still_video = next_still_video
    def is_running(self) -> bool:
        return self.process.poll() is None

    def close(self) -> None:
        if self.is_running():
            log.info(f"{self.stream_name}: stopping server")
            self.process.kill()

    def start_server(self, file_name_initial_video: Union[str, Path]) -> None:
        log.debug(f"{self.stream_name}: starting server with {file_name_initial_video}")
        self._make_concat_files()
        self.add_video(file_name_initial_video, still_only=True)
        # stills left by earlier servers for this camera are never played again and were never deleted
        for old_still in PATH_VIDEOS.glob(f"{self.stream_name_sanitized}_still_*.mp4"):
            if old_still != self.current_still_video:
                old_still.unlink(missing_ok=True)
        url = self._run_server()

        log.info(f"{self.stream_name}: stream ready at {url}")
