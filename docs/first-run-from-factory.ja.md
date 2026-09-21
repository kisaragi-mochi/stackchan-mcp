# 初回セットアップ: 出荷時 StackChan → Cursor

[English](first-run-from-factory.md) | 日本語

公式 M5Stack StackChan キットが出荷時ファームウェアのまま残っていて、
Cursor / Claude Code など MCP クライアントから操りたいときの手順です。

ルート README の[クイックスタート](../README.ja.md#クイックスタート)は短い版です。
このページは、机の上で実際につまずく箇所を残したものです。

## 完了時に揃うもの

- ロボット上の stackchan-mcp ファームウェア
- 手元マシンのゲートウェイ `ws://<LAN-IP>:8765/`
- Cursor などから `set_avatar` / `move_head` / `say` を呼べる状態
- 任意: [`examples/classic-avatar/`](../examples/classic-avatar/) のクラシック顔

## 安全

- USB-C は**顔側ではなくベース**へ。顔側だと書き込み中に首が動くことがあります。
- サーボ通電中に首を手で無理に回さない。
- pitch は **5–85°** に収める。
- Wi-Fi は **2.4 GHz のみ**。5 GHz には繋がりません。

## 1. 出荷時ペアリングを解除する

工場出荷の XiaoZhi（StackChan World）と本ファームウェアは互換ではありません。
焼く**前に**解除してください。

本体: **Setup → Account unbinding** のあと再起動。

または **StackChan World** アプリ: 該当デバイスの設定 → **Device unbinding**。

[xiaozhi.me](https://xiaozhi.me/) でもペアしたことがある場合は、そちらでも解除。

出荷時セットアップを一度も完了していなければ、この手順は不要です。

## 2. ファームウェアを焼く

1. 最新の [`firmware-v*`](https://github.com/kisaragi-mochi/stackchan-mcp/releases)
   から `merged-binary.bin` を取る。`0x0` へのクリーン書き込みは Wi-Fi を消します。想定どおりです。
2. `esptool` を入れる (`uv tool install esptool` または `pipx install esptool`)。
3. 電源を短押し。ポートが出なければ microSD 横の **RST** を 3 秒、LED が緑になるまで押して離す。
4. 書き込み:

```bash
esptool --chip esp32s3 --port /dev/cu.usbmodemXXXX -b 460800 \
  write_flash 0x0 merged-binary.bin
```

Linux は `/dev/ttyACM0` や `/dev/ttyUSB0`、Windows は `COMn`。

工場出荷に戻すときは M5Burner で **StackChan** を検索し **Only Official** にチェックして Burn。

## 3. 先にゲートウェイを起動する

公開パッケージを入れ、Cursor とスクリプトが共有する daemon モードで起動します。

```bash
uv tool install 'stackchan-mcp[tts]'
# Docker なしの英語 TTS（任意。ffmpeg が必要）:
uv tool install --force --with edge-tts 'stackchan-mcp[tts]'

export VISION_HOST=<このマシンのLAN-IP>   # 例: 192.168.0.169
stackchan-mcp serve --transport streamable-http --no-mdns
```

macOS では `--no-mdns` を推奨します。デフォルトの mDNS 広告は、ロボットの
セットアップ用ホットスポットと重なったときに Apple Wi-Fi (Skywalk) の
メモリを食い尽くし、Mac mini でカーネルパニックになった実例があります。
その場合、手順 4 でゲートウェイ URL を手入力する必要があります。

`http://127.0.0.1:8767/healthz` が `{"ok":true}` を返せば準備完了です。

LAN IP は macOS なら `ipconfig getifaddr en0`、Linux なら `ip addr`。

## 4. Wi-Fi はパソコンではなくスマホで

ゲートウェイを動かしている Mac / PC からロボットのホットスポットに
繋がないでください。

1. 焼き直後の画面は**歯車**だけ、で正常です。顔はゲートウェイ接続後に出ます。
2. ロボットが設定モード（歯車 / 「ホットスポットに接続」）になったら **スマホ**で。
3. `Xiaozhi-…` に接続。「インターネットなし」はそのままでよい。
4. ポータルが開かなければ `http://192.168.4.1`。
5. 自宅の **2.4 GHz** Wi-Fi を入れる。
6. **Advanced** を開く。
7. **WebSocket Gateway URL** に次を入れる（末尾スラッシュ含む）:

   `ws://<このマシンのLAN-IP>:8765/`

   ゲートウェイ側で `STACKCHAN_TOKEN` を入れていなければ Token は空でよい。
8. **Save では再起動しません。** 想定どおりです。**RST** か電源の短押しで再起動し、
   起動中は**画面に触らない**。

短押しは 1 秒未満のタップです。左の電源を 6 秒押し続けると**電源オフ**です。

### Advanced を忘れてドット顔のままのとき

Wi-Fi アイコンと正しい時刻が出ていれば LAN には入れています。
`esp32_connected: false` のままの小さなドットは、URL 未保存です。

設定モードに戻す:

1. 指を画面の上に構える。
2. **RST** を押す。
3. バックライトがついた瞬間 — 顔も Wi-Fi アイコンも出る前 — に画面を
   0.5 秒未満で一度タップ。

遅いとドット顔（idle）か、赤い listen LED になります。RST して、もっと早くタップ。

そのあとスマホのポータルで **Advanced** URL → Save → 画面を触らず RST。

## 5. 接続を確認する

```bash
curl -s http://127.0.0.1:8767/status
```

`"connected": true` と、0 でない `tools_count` が欲しい状態です。

最初の顔は小さな idle アイコンになりがちです。`set_avatar` で
`idle` / `happy` / … を指定すると名前つき表情に切り替わります。
音量の初期値はだいたい 70。机の上なら `set_volume` 90 が聞きやすいです。

## 6. Cursor を daemon に向ける

`~/.cursor/mcp.json`（またはプロジェクトの `.cursor/mcp.json`）:

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "url": "http://127.0.0.1:8767/mcp"
    }
  }
}
```

`STACKCHAN_TOKEN` を付けている場合は
`"headers": { "Authorization": "Bearer <token>" }` を足す。

MCP を再読み込みしたあと、エージェントに顔・首・発話を頼めます。

同じ設定で `stackchan-mcp` を stdio でも起動しないでください。
ESP32 のソケットは daemon が持ちます。

## 7. 任意: 英語の発話

デフォルトエンジン VOICEVOX は日本語です。英語なら `[tts]` extra と
`edge-tts` を入れ、`ffmpeg` を PATH に置いて:

```bash
export STACKCHAN_TTS_ENGINE=edge-tts
export STACKCHAN_EDGE_TTS_DEFAULT_VOICE=en-GB-SoniaNeural
```

`say` にテキストを渡します。本文の対応 emoji でも表情は変わりますが、
`set_avatar` を名前で呼べば emoji は不要です。

## 7b. 任意: 英語の聞き取り

`listen()` はこのマシンで文字起こしします。TTS と一緒に STT extra を入れ、
言語のデフォルトを英語にして daemon を再起動します:

```bash
export STACKCHAN_LISTEN_LANGUAGE=en
# このチェックアウトのゲートウェイ:
uv run --extra tts --extra stt-faster-whisper --with edge-tts \
  stackchan-mcp serve --transport streamable-http --no-mdns
```

初回は Whisper `base` モデル（約 140 MB）をダウンロードします。あとは
Cursor から「聞いて答えて」と頼めます。`language` には **スキーマ default
がありません** — 省略すると `STACKCHAN_LISTEN_LANGUAGE` が使われます。
一回だけ日本語なら `language="ja"` を付けてください。

## 8. 任意: クラシックなスタックチャン顔

ファームウェアの idle は小さなアイコンです。大きな両目のクラシック顔は
90 フレームの matrix です。PSRAM 上にあるので、ロボット再起動で消えます。

一度ビルドし、ゲートウェイにパスを渡せば **デバイスが繋がるたび**
（プロセス起動後の再接続も含む）に自動で載ります。このパスが設定されている
ときだけ、同じフックで瞬き（blink）も戻します。ファームウェア起動時は
blink がオフです:

```bash
uv run --with pillow python examples/classic-avatar/make_classic.py
export STACKCHAN_AVATAR_SET_PATH="$PWD/examples/classic-avatar/classic-matrix.rgb565"
```

mode は厳密なファイルサイズから推定します（ここでは `matrix`）。
Wi-Fi 省電力で 3.3 MB の取得が 180 秒に収まらないときは
`STACKCHAN_AVATAR_SET_TIMEOUT` を上げてください。サイズが layered /
matrix のいずれでもない場合は `STACKCHAN_AVATAR_SET_MODE` を明示してください
（ゲートウェイは推測しません）。

この環境変数は今のチェックアウトに入っています。PyPI の公開ゲートウェイは
次のリリースから対応します。それまではこのツリーから起動するか、
一回だけ手動で載せてください:

```bash
uv run --with pillow python examples/classic-avatar/load_classic.py
```

詳細は [`examples/classic-avatar/README.md`](../examples/classic-avatar/README.md)。

## Cursor hooks（任意）

同じ HTTP MCP を user hook から呼べます。ロボットがオフでもエディタを
止めないよう **fail open** にしてください。hook ファイルはこのリポジトリには
含めません。`~/.cursor/hooks.json` に置きます。

例:

- `sessionStart` → `set_avatar happy` と少し見上げる
- テスト失敗の `afterShellExecution` → `set_avatar sad`

## 工場出荷に戻す

M5Burner → StackChan → Only Official → Burn。
本ファームウェア側で xiaozhi.me に載せた場合は先にそちらを解除してから
StackChan World に再バインドします。
