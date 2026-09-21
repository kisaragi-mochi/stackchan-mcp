# Audio hook receiver

[日本語](README.ja.md)

Optional HTTP receiver for **device-driven** listen when you want the
Ogg/Opus POST to leave the gateway. For a single-machine desk setup,
prefer `STACKCHAN_AUDIO_HOOK_URL=local` instead — the gateway
transcribes and speaks in-process, with no second server.

This is not the Cursor `listen()` path. That one is already agent-started
and does not use this URL.

## Run

Gateway extra `[stt-faster-whisper]` must be installed (same as
`listen()`). Then:

```bash
export STACKCHAN_AUDIO_HOOK_URL=http://127.0.0.1:8780/audio
# restart the gateway so it picks up the URL
cd gateway
uv run --extra stt-faster-whisper python ../examples/audio-hook-receiver/receive.py
```

On the robot: short-tap the screen (red LED), speak, tap again to stop.
The first transcript downloads the Whisper model if it is not cached.

Bind/port overrides: `STACKCHAN_AUDIO_HOOK_BIND` (default `127.0.0.1`),
`STACKCHAN_AUDIO_HOOK_PORT` (default `8780`). Language and model follow
`STACKCHAN_LISTEN_LANGUAGE` and `STACKCHAN_FASTER_WHISPER_*`
(language defaults to `ja`, matching gateway `listen()`).

If `STACKCHAN_AUDIO_HOOK_TOKEN` or `STACKCHAN_TOKEN` is set, POSTs must
send the same Bearer token.

The default spoken reply is `You said: …`. There is no LLM in this
example — swap `reply_text()` if you want a smarter answer.
