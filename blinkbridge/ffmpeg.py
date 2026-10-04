import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple, Union

from blinkbridge.config import COMMON_FFMPEG_ARGS

log = logging.getLogger(__name__)
# None of the ffprobe/ffmpeg waits below used to have a deadline, so one wedged child process
# blocked the still builder for good.
SUBPROCESS_TIMEOUT = 60


def _communicate(process, what: str):
    try:
        return process.communicate(timeout=SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            log.error(f"{what} ignored kill, leaving it to the OS")
        raise Exception(f"{what} did not finish within {SUBPROCESS_TIMEOUT} s, killed")

class StreamParameters:
    def __init__(self, video_file: Union[str, Path]):
        ffprobe_params = [
            'ffprobe',
            '-hide_banner',
            '-loglevel', 'fatal',
            '-show_streams',
            '-print_format', 'json',
            video_file
        ]

        self.process = subprocess.Popen(ffprobe_params, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def wait(self) -> Tuple[Dict, Dict]:
        out, err = _communicate(self.process, 'ffprobe')

        if self.process.returncode != 0:
            raise Exception("ffprobe failed to extract parameters: " + err.decode('utf-8'))

        # convert json but keep floats and ints as strings
        js = json.loads(out.decode('utf-8'), parse_float=lambda x: x, parse_int=lambda x: x)
        js = js['streams']

        stream_audio = next((s for s in js if s['codec_name'] == 'aac'), {})
        stream_video = next((s for s in js if s['codec_name'] == 'h264'), {})

        return stream_audio, stream_video

class VideoToLastFrame:
    def __init__(self, input_video: Union[str, Path], output_image: Union[str, Path]):
        time_offset_from_end = 1.0

        ffmpeg_params = [
            'ffmpeg',
            *COMMON_FFMPEG_ARGS,
            '-sseof', str(-time_offset_from_end),
            '-i', input_video,
            '-update', '1',
            '-pix_fmt', 'yuv420p',
            '-vf', 'scale=out_range=pc',  # HACK
            '-q:v', '1',
            output_image
        ]

        self.process = subprocess.Popen(ffmpeg_params, stdout=sys.stdout, stderr=subprocess.PIPE)

    def wait(self) -> None:
        out, err = _communicate(self.process, 'ffmpeg extracting the last frame')

        if self.process.returncode != 0:
            raise Exception("ffmpeg failed to extract the last frame: " + err.decode('utf-8'))

class FrameToVideo:
    def __init__(self,
                 image_file_name: Union[str, Path],
                 params_video: Dict,
                 params_audio: Dict,
                 output_duration: float=1,
                 file_name_output_video: Union[str, Path]="output.mp4"):
        time_base_denominator = params_video['time_base'].split('/')[1] # cut off "1/"
        fps_value = params_video['r_frame_rate']

        # Create the ffmpeg parameters list
        ffmpeg_params = [
            'ffmpeg',
            *COMMON_FFMPEG_ARGS,
            '-loop', '1',
            '-i', image_file_name,
            '-f', 'lavfi',
            '-i', f"anullsrc=channel_layout={params_audio['channels']}:sample_rate={params_audio['sample_rate']}",
            '-c:v', params_video['codec_name'],
            '-pix_fmt', params_video['pix_fmt'],
            '-t', str(output_duration),
            '-vf', f"scale={params_video['width']}:{params_video['height']},fps={fps_value}",
            '-b:v', params_video['bit_rate'],
            '-profile:v', params_video['profile'],
            '-level:v', params_video['level'],
            '-movflags', 'faststart',
            '-video_track_timescale', time_base_denominator,
            '-fps_mode', 'passthrough',
            '-c:a', 'aac',
            '-ar', params_audio['sample_rate'],
            '-ac', params_audio['channels'],
            file_name_output_video
        ]

        # Create the video using ffmpeg
        self.process = subprocess.Popen(ffmpeg_params, stdout=sys.stdout, stderr=subprocess.PIPE)

    def wait(self) -> None:
        out, err = _communicate(self.process, 'ffmpeg creating the still video')

        if self.process.returncode != 0:
            raise Exception(f"ffmpeg failed to create the video: {err.decode('utf-8')}")

def make_still_video(file_name_input_video: Union[str, Path],
                     output_duration: float=1,
                     file_name_still_video: Union[str, Path]="output.mp4") -> None:
    '''
    Make a short looping video from the last frame of a clip.

    Synchronous and blocking: the caller runs it in a worker thread (asyncio.to_thread), never on
    the event loop. The three children run one after another, so at most one exists at a time and
    a failure anywhere leaves nothing running. This used to be a class that started its own thread
    and ran two children at once: the last-frame ffmpeg started before ffprobe was awaited, so when
    ffprobe failed or timed out its wait was skipped and that ffmpeg kept running, unowned. The
    thread's join was also the event loop's join, which stalled every camera while it waited.
    '''
    # one temporary image per still (it was a single last_frame.jpg shared by all cameras)
    still_image_file_name = Path(file_name_still_video).with_suffix('.jpg')
    current = None
    try:
        current = StreamParameters(file_name_input_video)
        params_audio, params_video = current.wait()

        if not (params_audio and params_video):
            raise ValueError(f"{file_name_input_video} has no AAC audio or no H.264 video stream")

        current = VideoToLastFrame(file_name_input_video, still_image_file_name)
        current.wait()

        current = FrameToVideo(still_image_file_name, params_video, params_audio,
                               output_duration=output_duration,
                               file_name_output_video=file_name_still_video)
        current.wait()
    finally:
        # each wait already kills its own child on a timeout; this covers any other way out,
        # because a stray ffmpeg on this box competes with the router and every camera
        if current is not None and current.process.poll() is None:
            current.process.kill()
            try:
                current.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.error("still builder child ignored kill, leaving it to the OS")
        still_image_file_name.unlink(missing_ok=True)
