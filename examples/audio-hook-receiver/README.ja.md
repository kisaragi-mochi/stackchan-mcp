# Audio hook レシーバー

[English](README.md)

ゲートウェイから Ogg/Opus の POST を別プロセスで受けたいときの、
**デバイス駆動** listen 向けのオプション HTTP レシーバーです。
1 台のデスク構成なら `STACKCHAN_AUDIO_HOOK_URL=local` の方が簡単です。
ゲートウェイ自身が文字起こしして喋り、第 2 サーバは不要です。

これは Cursor の `listen()` 経路ではありません。そちらはエージェントが
起動するもので、この URL は使いません。

## 実行

ゲートウェイに `[stt-faster-whisper]` extra が入っている必要があります
（`listen()` と同じ）。そのうえで:

```bash
export STACKCHAN_AUDIO_HOOK_URL=http://127.0.0.1:8780/audio
# URL を拾うためにゲートウェイを再起動
cd gateway
uv run --extra stt-faster-whisper python ../examples/audio-hook-receiver/receive.py
```

ロボット側: 画面を短くタップ（赤い LED）→ 話す → もう一度タップして停止。
初回の文字起こしでは、未キャッシュなら Whisper モデルをダウンロードします。

バインド / ポートの上書き: `STACKCHAN_AUDIO_HOOK_BIND`（既定 `127.0.0.1`）、
`STACKCHAN_AUDIO_HOOK_PORT`（既定 `8780`）。言語とモデルは
`STACKCHAN_LISTEN_LANGUAGE` と `STACKCHAN_FASTER_WHISPER_*` に従います
（言語の既定は `ja`。ゲートウェイの `listen()` と同じ）。

`STACKCHAN_AUDIO_HOOK_TOKEN` または `STACKCHAN_TOKEN` が設定されている場合、
POST は同じ Bearer トークンを送る必要があります。

既定の発話は `You said: …` です。この example に LLM はありません。
もっと賢い返答が欲しければ `reply_text()` を差し替えてください。
