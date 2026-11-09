"""Experimental one-shot playback on an already connected IMMI live view."""

import asyncio
import struct
import subprocess

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1024
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
FRAME_TICKS = FRAME_SAMPLES * 90_000 // SAMPLE_RATE
MAX_SECONDS = 17


def encode_wav(audio: bytes) -> list[bytes]:
    """Encode and split at ADTS boundaries before touching the camera."""
    encoded = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "wav",
            "-i", "pipe:0", "-t", str(MAX_SECONDS + 1),
            "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "aac", "-profile:a", "aac_low", "-aac_pns", "0",
            "-b:a", "40k", "-f", "adts", "pipe:1",
        ],
        input=audio,
        stderr=subprocess.PIPE,
        check=True,
        stdout=subprocess.PIPE,
        timeout=15,
    ).stdout
    frames = []
    offset = 0
    while offset < len(encoded):
        header = encoded[offset : offset + 7]
        if len(header) != 7 or header[0] != 0xFF or header[1] & 0xF0 != 0xF0:
            raise ValueError("Invalid ADTS header")
        size = ((header[3] & 3) << 11) | (header[4] << 3) | (header[5] >> 5)
        if size <= 7 or offset + size > len(encoded):
            raise ValueError("Invalid ADTS frame length")
        frames.append(encoded[offset : offset + size])
        offset += size
    # FFmpeg's AAC encoder adds one priming frame and pads the last frame.
    if not frames or (len(frames) - 2) * FRAME_SECONDS >= MAX_SECONDS:
        raise ValueError(f"Expected a nonempty WAV of at most {MAX_SECONDS} seconds")
    return frames


async def play_wav(writer: asyncio.StreamWriter, audio: bytes) -> int:
    """Send one WAV on an exclusive, authenticated IMMI session."""
    frames = await asyncio.to_thread(encode_wav, audio)

    async def send(kind: int, sequence: int, payload: bytes = b"") -> None:
        if writer.is_closing():
            raise ConnectionError("IMMI live view closed")
        writer.write(struct.pack(">BII", kind, sequence, len(payload)) + payload)
        await asyncio.wait_for(writer.drain(), timeout=1)

    try:
        await send(0x17, 3)  # Start audio
        for index, frame in enumerate(frames):
            await send(0x05, index * FRAME_TICKS, frame)
            await asyncio.sleep(FRAME_SECONDS)
    finally:
        if not writer.is_closing():
            await send(0x17, 4)  # Stop audio
    return len(frames)
