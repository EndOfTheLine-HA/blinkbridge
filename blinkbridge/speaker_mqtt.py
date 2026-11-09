"""Optional MQTT speaker endpoint shared with Blink Camera Messages."""

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import ssl
import time

import aiomqtt

from blinkbridge.live import LiveStreamServer
from blinkbridge.speaker import play_wav

log = logging.getLogger(__name__)
MAX_PAYLOAD = 2_200_000


async def run(app, hostname):
    """Serve correlated discovery and playback requests on the shared broker."""
    prefix = os.getenv("BLINKBRIDGE_MQTT_PREFIX", "blinkbridge").rstrip("/")
    if not prefix or any(c in prefix for c in "+#\0"):
        log.error("Invalid BlinkBridge MQTT topic prefix")
        return
    topic = f"{prefix}/speaker/v1"
    seen = {}
    while True:
        try:
            async with aiomqtt.Client(
                hostname,
                port=int(os.getenv("BLINKBRIDGE_MQTT_PORT", "1883")),
                username=os.getenv("BLINKBRIDGE_MQTT_USERNAME"),
                password=os.getenv("BLINKBRIDGE_MQTT_PASSWORD"),
                tls_context=ssl.create_default_context()
                if os.getenv("BLINKBRIDGE_MQTT_TLS") == "1" else None,
                max_queued_incoming_messages=8,
            ) as client:
                await client.subscribe([(f"{topic}/discover", 1), (f"{topic}/play", 1)])
                async for message in client.messages:
                    if message.retain or len(message.payload) > MAX_PAYLOAD:
                        continue
                    try:
                        request = json.loads(message.payload)
                    except (ValueError, UnicodeError):
                        continue
                    request_id = request.get("id") if isinstance(request, dict) else None
                    if not isinstance(request_id, str) or not re.fullmatch("[0-9a-f]{32}", request_id):
                        continue
                    if request_id not in seen:
                        seen[request_id] = await respond(app, str(message.topic), request)
                        if len(seen) > 256:
                            del seen[next(iter(seen))]
                    await client.publish(
                        f"{topic}/result/{request_id}", json.dumps(seen[request_id]), qos=1
                    )
        except (OSError, aiomqtt.MqttError):
            log.warning("Speaker MQTT connection unavailable, retrying in 5 seconds")
            await asyncio.sleep(5)


async def respond(app, topic, request):
    cameras = {
        str(camera.camera_id): (name, app.stream_servers.get(name))
        for name, camera in app.cam_manager.blink.cameras.items()
        if isinstance(app.stream_servers.get(name), LiveStreamServer)
    }
    if topic.endswith("/discover"):
        return {"cameras": [{"id": key, "name": name} for key, (name, _) in cameras.items()]}

    deadline = request.get("deadline")
    now = time.time()
    if type(deadline) not in (int, float) or not now < deadline <= now + 45:
        return {"status": "error", "error": "Playback request expired"}

    camera_id = request.get("camera")
    if not isinstance(camera_id, str) or camera_id not in cameras:
        return {"status": "error", "error": "Camera is not configured for BlinkBridge live view"}
    name, server = cameras[camera_id]
    writer = server.speaker_writer
    if writer is None or writer.is_closing():
        return {"status": "error", "error": "Camera has no active IMMI live view"}
    try:
        audio = base64.b64decode(request["audio"], validate=True)
        async with asyncio.timeout(min(32, deadline - now)):
            count = await play_wav(writer, audio)
        log.info("%s: speaker message sent (%d AAC frames)", name, count)
        return {"status": "ended"}
    except (KeyError, TypeError, binascii.Error):
        return {"status": "error", "error": "Invalid WAV request"}
    except Exception:
        log.warning("%s: speaker playback failed", name, exc_info=True)
        return {"status": "error", "error": "BlinkBridge could not play this message"}
