"""Edge TTS engine — subprocess client for Microsoft's online TTS service.

Uses the `edge-tts` Python package's CLI (https://github.com/rany2/edge-tts)
to synthesise speech via Microsoft's online TTS service. No local model or
Docker container needed — just `pip install edge-tts` and the `edge-tts`
binary on PATH. Produces natural English (and many other language) voices,
unlike VOICEVOX which is Japanese-only by default.

edge-tts always emits MP3 regardless of the output filename/extension
passed to --write-media, so this engine pipes that MP3 through ffmpeg to
get raw 16 kHz mono PCM.

This is intentionally a subprocess-based engine (not an HTTP client like
VoicevoxEngine) since edge-tts ships as a CLI tool, not a long-running
server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .audio_utils import DEVICE_SAMPLE_RATE
from .base import TTSEngine

logger = logging.getLogger(__name__)


#: Default voice. en-GB-SoniaNeural is a natural British English voice.
#: Override per-call via the say() tool's `speaker_id` opt (repurposed here
#: as a voice name string) or globally via STACKCHAN_EDGE_TTS_DEFAULT_VOICE.
DEFAULT_EDGE_TTS_VOICE = "en-GB-SoniaNeural"

#: Timeout for the edge-tts subprocess and the ffmpeg conversion step.
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 30.0


class EdgeTTSEngine(TTSEngine):
    """Synthesise text via the edge-tts CLI, return 16 kHz mono PCM.

    Setup: `pip install edge-tts` (already on PATH as `edge-tts` once
    installed) and `ffmpeg` on PATH for MP3 -> PCM conversion.

    Configuration:

        STACKCHAN_EDGE_TTS_DEFAULT_VOICE
            Voice name used when the say() tool does not specify one.
            Default "en-GB-SoniaNeural".
    """

    name = "edge-tts"

    def __init__(
        self,
        default_voice: str | None = None,
        timeout_seconds: float = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        edge_tts_binary: str | None = None,
        ffmpeg_binary: str | None = None,
    ) -> None:
        env_voice = os.getenv("STACKCHAN_EDGE_TTS_DEFAULT_VOICE")
        self._default_voice = default_voice or env_voice or DEFAULT_EDGE_TTS_VOICE
        self._timeout_seconds = timeout_seconds
        self._edge_tts_binary = edge_tts_binary or "edge-tts"
        self._ffmpeg_binary = ffmpeg_binary or "ffmpeg"

    @property
    def default_voice(self) -> str:
        """Voice name used when no voice is specified per-call."""
        return self._default_voice

    async def synthesize(self, text: str, **opts: Any) -> bytes:
        """Run edge-tts + ffmpeg, return 16 kHz mono signed-16-bit PCM.

        Recognised opts:

            voice: str
                Edge TTS voice name (e.g. "en-GB-SoniaNeural",
                "en-US-AriaNeural"). Falls back to `default_voice`.
            speaker_id: str
                Alternate way to pass the Edge voice name, matching the
                say() tool's generic per-engine option naming.
        """
        if not text:
            raise ValueError("text must not be empty")

        voice = opts.get("voice") or opts.get("speaker_id") or self._default_voice

        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_path = Path(tmpdir) / "out.mp3"
            pcm_path = Path(tmpdir) / "out.pcm"

            proc = await asyncio.create_subprocess_exec(
                self._edge_tts_binary,
                "--voice", voice,
                "--text", text,
                "--write-media", str(mp3_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_seconds
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"edge-tts failed (code {proc.returncode}): "
                    f"{stderr.decode(errors='replace')}"
                )

            ffmpeg_proc = await asyncio.create_subprocess_exec(
                self._ffmpeg_binary, "-y", "-i", str(mp3_path),
                "-f", "s16le", "-ar", str(DEVICE_SAMPLE_RATE), "-ac", "1",
                str(pcm_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, ffmpeg_stderr = await asyncio.wait_for(
                ffmpeg_proc.communicate(), timeout=self._timeout_seconds
            )
            if ffmpeg_proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg conversion failed (code {ffmpeg_proc.returncode}): "
                    f"{ffmpeg_stderr.decode(errors='replace')}"
                )

            pcm = pcm_path.read_bytes()

        logger.info(
            "edge-tts synthesised %d bytes PCM (16 kHz mono) for "
            "voice=%s, text=%r",
            len(pcm),
            voice,
            text[:60],
        )
        return pcm
