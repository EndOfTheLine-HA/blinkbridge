# BlinkBridge release candidate

BlinkBridge turns Blink camera clips into persistent RTSP streams by publishing
each new clip and then looping its last frame through FFmpeg and MediaMTX. This
branch also contains an optional, experimental Blink Live View relay.

> **Distribution is not cleared.** The upstream
> [`roger-/blinkbridge`](https://github.com/roger-/blinkbridge) repository has
> no explicit license. This private release candidate must not be published or
> redistributed until the upstream author grants suitable permission or adds a
> license. See [LICENSE_STATUS.md](LICENSE_STATUS.md).

This project is unofficial and is not affiliated with Blink or Amazon. Live
View and aggressive polling can increase battery use, load Blink's service,
or trigger account throttling. The example uses a 60-second poll interval.

## What differs from upstream

- Blink/network failures no longer tear down a still-running RTSP stream.
- OAuth refresh tokens are saved atomically with owner-only permissions.
- Missing clips and failed camera starts are retried without stopping the
  monitor loop.
- FFprobe and FFmpeg work has deadlines, and blocking still-frame work runs
  outside the async event loop.
- RTSP publishing uses TCP, failed streams have bounded restart behavior, and
  stale still files are removed.
- New clips missed by BlinkPy's motion flag can be recovered from the media
  response already fetched by BlinkPy.
- Optional Live View sessions can publish continuously, hand over before a
  session expires, and fall back to clips after repeated failures.
- Logs redact common tokens, passwords, and email addresses.

## Private test setup

1. Copy the example to the ignored runtime path:

   ```sh
   cp config/config.example.json config/config.json
   ```

2. Put your Blink username and password in the untracked `config/config.json`.
   Alternatively, set `BLINK_USERNAME` and `BLINK_PASSWORD` in an untracked
   `.env` file. Docker environment values remain visible to local Docker
   administrators, so neither option protects against a host administrator.

3. Keep `cameras.live` empty for clip/still mode. To test Live View, list only
   the exact camera names you intend to keep awake. `cameras.live_handover`
   enables overlapping renewals and should be introduced cautiously.

4. Build and perform the one-time interactive login:

   ```sh
   docker compose build
   docker compose run --rm blinkbridge
   ```

   Enter the verification code if prompted, then stop the process. The OAuth
   state is written to ignored `config/.cred.json` with mode `0600`.

5. Start the services:

   ```sh
   docker compose up -d
   docker compose logs -f blinkbridge
   ```

The default RTSP publication is bound to `127.0.0.1:8554`. If a client on
another machine must connect, set `BLINKBRIDGE_RTSP_BIND` deliberately and add
MediaMTX authentication and firewall restrictions before exposing the port.

## Configuration notes

- `cameras.enabled`: empty means all cameras except `cameras.disabled`.
- `cameras.live`: exact camera names that use the experimental Live View relay.
- `cameras.live_handover`: Live View cameras that open the next session before
  the current one ends.
- `cameras.live_handover_after`: seconds before handover, or a mapping from
  camera name to seconds.
- `blink.refresh_minutes`: when nonzero, request a fresh clip after a camera
  has been quiet this long. This sends recording commands and is disabled by
  default.
- `blink.refresh_cameras`: optional allowlist for those recording commands.

## Validation

Run the local unit and publication-hygiene checks with:

```sh
python -m pip install -r requirements-dev.txt
ruff check blinkbridge tests scripts
python -m unittest discover -s tests -v
python scripts/check_public_tree.py
```

The checks do not contact Blink, start Live View, or validate real cameras.
A private end-to-end trial is still required before any release.
