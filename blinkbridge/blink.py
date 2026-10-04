import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Union

import blinkpy.api as blink_api
from aiohttp import ClientSession
from blinkpy.auth import Auth, BlinkTwoFARequiredError, LoginError, TokenRefreshFailed
from blinkpy.blinkpy import Blink
from blinkpy.helpers.util import json_load

from blinkbridge.config import CONFIG, PATH_CONFIG, PATH_VIDEOS
from blinkbridge.utils import sanitize_stream_name

log = logging.getLogger(__name__)


def find_most_recent_clip_url(recent_clips: dict, date: str) -> str:
    # sort data in reverse order by time
    sorted_data = sorted(recent_clips, key=lambda x: x['time'], reverse=True)

    # get the first entry that does not contain "/snapshot/"
    for entry in sorted_data:
        if '/snapshot/' not in entry['clip']:
            break
    else:
        return ''

    # convert to datetime
    date = datetime.fromisoformat(date.replace('Z', '+00:00'))
    entry_time = datetime.fromisoformat(entry['time'].replace('Z', '+00:00'))

    # see if entry is newer than date
    if entry_time > date:
        return entry['clip']

    return ''

def write_file_atomic(file_name: Path, data: bytes, mode: int = 0o644) -> None:
    # Write to a temporary name and rename it over the target. ffmpeg may still be playing the old
    # clip: a rename leaves its open file intact, while writing in place truncated the file under
    # ffmpeg, and it also made "wait until ffmpeg opens the new clip" succeed at once.
    tmp = file_name.with_name(file_name.name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, file_name)

def parse_blink_time(value) -> Union[datetime, None]:
    # Blink timestamps look like 2026-09-27T06:50:15+00:00
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def is_playable_clip(media: dict) -> bool:
    return bool(media.get('media')) and not media.get('deleted') and media.get('source') != 'snapshot' and not media.get('partial')

class CameraManager:
    def __init__(self):
        self.session = ClientSession()
        self.camera_last_record = defaultdict(lambda: None)
        self.metadata = None
        self.saved_tokens = None
        # newest clip played per camera, and the clips seen in recent Blink video lists
        self.last_clip_time = {}
        self.media_recent = {}
        self.media_capture_active = False

    async def _login(self) -> None:
        """Login to Blink using OAuth v2 authentication."""
        self.blink = Blink(session=self.session)
        self._install_media_capture()
        path_cred = PATH_CONFIG / ".cred.json"
        path_needs_2fa = PATH_CONFIG / ".needs-2fa"

        if path_needs_2fa.exists():
            # A 2FA code was requested earlier and nobody entered it. Every new login attempt sends
            # another code, and repeated logins can lock a Blink account, so do not try again
            # until someone deletes this file and logs in interactively
            raise LoginError(f"{path_needs_2fa} exists, Blink is waiting for a 2FA login: delete it and run 'docker compose run blinkbridge'")

        if not path_cred.exists():
            log.info("Logging into Blink with credentials from config")
            # Create Auth with login credentials for initial OAuth v2 flow
            self.blink.auth = Auth(CONFIG['blink']['login'], no_prompt=True, session=self.session)
        else:
            log.info("Logging into Blink with saved credentials")
            # Load saved credentials (includes OAuth tokens, hardware_id, etc.)
            saved_data = await json_load(path_cred)
            self.blink.auth = Auth(saved_data, no_prompt=True, session=self.session)

        try:
            # Start the Blink system (performs OAuth v2 authentication). blinkpy catches its own
            # LoginError / TokenRefreshFailed / BlinkSetupError and only returns False, which used
            # to be logged as a success (and then saved over .cred.json)
            if await self.blink.start() is False:
                raise LoginError("Blink setup failed, see the blinkpy errors above")
            log.info("Successfully authenticated with Blink")
        except BlinkTwoFARequiredError:
            # Prompt user for 2FA code
            log.info("Two-factor authentication required")
            try:
                # the container normally has no one at the keyboard: wait at most 10 minutes
                twofa_code = await asyncio.wait_for(asyncio.to_thread(input, "Enter your 2FA code: "), timeout=600)
            except (EOFError, asyncio.TimeoutError):
                path_needs_2fa.touch(mode=0o600)
                raise LoginError("Blink asked for a 2FA code and none was entered, not logging in again until "
                                 f"{path_needs_2fa} is deleted and 'docker compose run blinkbridge' is run")

            # Complete 2FA login
            success = await self.blink.send_2fa_code(twofa_code)
            if not success:
                log.error("2FA verification failed")
                raise LoginError("2FA verification failed")

            log.info("Successfully authenticated with Blink (2FA completed)")
        except (TokenRefreshFailed, LoginError) as e:
            # The saved credentials are kept. A failure can be temporary (network, DNS, Blink
            # outage) and deleting them forced a new login with 2FA. Delete .cred.json by hand
            # and run 'docker compose run blinkbridge' if they really are invalid.
            log.error(f"Authentication failed: {e}")
            raise

        # Save credentials after successful authentication (includes OAuth tokens)
        log.info("Saving Blink credentials for future use")
        await self.save_credentials()

    async def save_credentials(self) -> None:
        path_cred = PATH_CONFIG / ".cred.json"
        data = json.dumps(self.blink.auth.login_attributes, indent=4).encode()
        stat = path_cred.stat() if path_cred.exists() else None
        write_file_atomic(path_cred, data, mode=0o600)
        if stat:
            # keep the owner of the existing file (the rename would otherwise make it root's)
            try:
                os.chown(path_cred, stat.st_uid, stat.st_gid)
            except OSError:
                pass
        self.saved_tokens = (self.blink.auth.token, self.blink.auth.refresh_token)

    def _install_media_capture(self) -> None:
        # blinkpy's check_new_videos() only keeps a clip whose created_at is later than the
        # previous refresh, so a clip that finishes uploading after that refresh is dropped for
        # good (most 30 s clips, which then never reach the stream). The video list response
        # still contains it, so keep every entry blinkpy fetches and look for new clips there.
        # This adds no Blink API calls.
        original = getattr(blink_api.request_videos, '__wrapped__', blink_api.request_videos)
        manager = self

        async def request_videos(blink, time=None, page=0):
            response = await original(blink, time=time, page=page)
            if isinstance(response, dict) and isinstance(response.get('media'), list):
                try:
                    manager._remember_media(response['media'])
                except Exception as e:
                    # this runs inside blinkpy's refresh, so it must never break it
                    log.debug(f"could not record Blink video list: {e}")
            return response

        request_videos.__wrapped__ = original
        blink_api.request_videos = request_videos

    def _remember_media(self, entries: list) -> None:
        now = datetime.now().astimezone()
        for entry in entries:
            created = parse_blink_time(entry.get('created_at')) if isinstance(entry, dict) else None
            if created is None or not entry.get('device_name'):
                continue
            self.media_recent[entry.get('id') or entry.get('media')] = entry
        # keep 15 minutes of entries, so a clip whose download failed is tried again next poll
        self.media_recent = {k: v for k, v in self.media_recent.items()
                             if now - parse_blink_time(v.get('created_at')) < timedelta(minutes=15)}
        self.media_capture_active = True

    async def request_clip(self, camera_name: str) -> None:
        # Ask Blink to record a clip now (what the Blink app's record button and Home Assistant's
        # blink.record do). The clip then arrives in the video list like a motion clip.
        response = await self.blink.cameras[camera_name].record()
        if not response:
            raise ValueError("Blink did not accept the record command")

    def seconds_since_last_clip(self, camera_name: str) -> Union[float, None]:
        last = self.last_clip_time.get(camera_name)
        if last is None:
            return None
        return (datetime.now(timezone.utc) - last).total_seconds()

    def _note_clip(self, camera_name: str, media: dict) -> None:
        created = parse_blink_time(media.get('created_at'))
        if created and (camera_name not in self.last_clip_time or created > self.last_clip_time[camera_name]):
            self.last_clip_time[camera_name] = created

    async def refresh(self) -> None:
        await self.blink.refresh()
        # blinkpy refreshes the OAuth token by itself and the new refresh token replaces the old
        # one, but only the login-time copy was ever written to .cred.json
        tokens = (self.blink.auth.token, self.blink.auth.refresh_token)
        if all(tokens) and tokens != self.saved_tokens:
            log.debug("Blink tokens changed, updating saved credentials")
            await self.save_credentials()

    async def refresh_metadata(self) -> None:
        log.debug('refreshing video metadata')
        dt_past = datetime.now() - timedelta(days=CONFIG['blink']['history_days'])
        try:
            metadata = await self.blink.get_videos_metadata(since=str(dt_past), stop=2)
        except Exception as e:
            log.warning(f"could not refresh video metadata, keeping the previous list: {e}")
            return
        if metadata:
            self.metadata = metadata
        else:
            log.warning("Blink returned no video metadata, keeping the previous list")

    async def save_latest_clip(self, camera_name: str, force: bool=False) -> Union[Path, None]:
        '''
        Download and save latest videos for camera
        '''
        camera_name_sanitized = sanitize_stream_name(camera_name)
        file_name = PATH_VIDEOS / f"{camera_name_sanitized}_latest.mp4"

        # don't download if clip already exists
        if file_name.exists() and not force:
            log.debug(f"{camera_name}: skipping download, {file_name} exists")
            return file_name

        # skip deleted clips and camera snapshots
        # newest by created_at, rather than trusting the order of Blink's list
        clips = [m for m in (self.metadata or []) if m.get('device_name') == camera_name
                 and is_playable_clip(m) and parse_blink_time(m.get('created_at'))]
        media = max(clips, key=lambda m: parse_blink_time(m.get('created_at')), default=None)

        try:
            if media is None:
                raise ValueError("no clip in Blink's video list")

            log.debug(f"{camera_name}: downloading video created at {media.get('created_at')}")
            response = await self.blink.do_http_get(media['media'])
            if response is None or response.status != 200:
                raise ValueError(f"download failed (HTTP {getattr(response, 'status', 'no response')})")
            data = await response.read()
            if not data:
                raise ValueError("download was empty")

            log.debug(f'{camera_name}: saving video to {file_name}')
            write_file_atomic(file_name, data)
            self._note_clip(camera_name, media)
        except Exception as e:
            # Fall back to the clip already on disk, so a restart during a Blink or network
            # problem still has something to play instead of failing
            if file_name.exists():
                log.warning(f"{camera_name}: could not download the latest clip ({e}), reusing {file_name}")
                return file_name
            log.warning(f"{camera_name}: no clips found for camera ({e})")
            return None

        return file_name

    async def _save_clip(self, camera_name: str, url: str, file_name: Path) -> None:
        camera = self.blink.cameras[camera_name]
        response = await camera.get_video_clip(url)
        if response is None or response.status != 200:
            raise ValueError(f"clip download failed (HTTP {getattr(response, 'status', 'no response')})")

        log.debug(f'{camera_name}: saving video to {file_name}')
        write_file_atomic(file_name, await response.read())

    async def check_for_motion(self, camera_name: str) -> Union[Path, None]:
        '''
        Check if a camera has been motion detected
        '''
        # Blink data is refreshed once per poll by refresh(), before the cameras are checked
        if self.media_capture_active:
            return await self._check_for_new_clip(camera_name)

        # Fallback if the video list capture is not working: blinkpy's own motion flag
        camera = self.blink.cameras[camera_name]

        if not camera.attributes['motion_detected'] or self.camera_last_record[camera_name] == camera.attributes['last_record']:
            return None

        log.debug(f"{camera_name}: motion detected at {camera.attributes.get('last_record')}")

        camera_name_sanitized = sanitize_stream_name(camera_name)
        file_name = PATH_VIDEOS / f"{camera_name_sanitized}_latest.mp4"

        # HACK: detect snapshot events and see if there is a recent clip in them
        if '/snapshot/' in camera.attributes['video']:
            if url := find_most_recent_clip_url(camera.attributes['recent_clips'], camera.attributes['last_record']):
                log.debug(f"{camera_name}: found recent clip in snapshot, saving to {file_name}")
                await self._save_clip(camera_name, url, file_name)
                self.camera_last_record[camera_name] = camera.attributes['last_record']

                return file_name

            log.debug(f"{camera_name}: no recent clip in snapshot, skipping")
            self.camera_last_record[camera_name] = camera.attributes['last_record']

            return None

        log.debug(f"{camera_name}: saving video to {file_name}")
        file_name_part = file_name.with_name(file_name.name + '.part')
        await camera.video_to_file(file_name_part)
        if not file_name_part.exists() or file_name_part.stat().st_size == 0:
            raise ValueError("clip download failed")
        os.replace(file_name_part, file_name)
        self.camera_last_record[camera_name] = camera.attributes['last_record']

        return file_name

    async def _check_for_new_clip(self, camera_name: str) -> Union[Path, None]:
        '''
        Play the newest clip of this camera that is newer than the last one played
        '''
        last = self.last_clip_time.get(camera_name)
        newest = None
        for media in self.media_recent.values():
            if media.get('device_name') != camera_name or not is_playable_clip(media):
                continue
            created = parse_blink_time(media.get('created_at'))
            if last is not None and created <= last:
                continue
            if newest is None or created > parse_blink_time(newest.get('created_at')):
                newest = media

        if newest is None:
            return None

        flagged = bool(self.blink.cameras[camera_name].attributes.get('motion_detected'))
        log.debug(f"{camera_name}: new clip {newest.get('created_at')} (blinkpy motion flag: {flagged})")

        camera_name_sanitized = sanitize_stream_name(camera_name)
        file_name = PATH_VIDEOS / f"{camera_name_sanitized}_latest.mp4"
        response = await self.blink.do_http_get(newest['media'])
        if response is None or response.status != 200:
            raise ValueError(f"clip download failed (HTTP {getattr(response, 'status', 'no response')})")
        data = await response.read()
        if not data:
            raise ValueError("clip download was empty")
        write_file_atomic(file_name, data)
        self._note_clip(camera_name, newest)
        if not flagged:
            log.info(f"{camera_name}: playing clip from {newest.get('created_at')} that blinkpy did not flag as motion")

        return file_name

    def get_cameras(self) -> iter:
        return self.blink.cameras.keys()

    async def start(self) -> None:
        await self._login()
        await self.refresh_metadata()
        # the newest clip per camera at startup counts as already played
        for media in (self.metadata or []):
            if media.get('device_name') and is_playable_clip(media):
                self._note_clip(media['device_name'], media)

    async def close(self) -> None:
        """Properly close all connections and clean up resources."""
        # Close the session only once (blink.auth.session is the same as self.session)
        if hasattr(self, 'session') and self.session is not None:
            if not self.session.closed:
                await self.session.close()
                # Give the event loop time to clean up SSL transports
                # This prevents "Unclosed connector" warnings
                await asyncio.sleep(0.25)

async def test() -> None:
    cm = CameraManager()

    await cm.start()
    await cm.refresh()

    for camera in cm.get_cameras():
        file_name = await cm.check_for_motion(camera)

        print(file_name)

    await cm.close()

if __name__ == "__main__":
    asyncio.run(test())
