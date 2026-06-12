[English](README.md) | **日本語**

# stackchan-mcp

**M5Stack 公式 [StackChan](https://docs.m5stack.com/ja/StackChan)** (2025年 Kickstarter 出荷キット) を任意の LLM クライアントから操作するための MCP (Model Context Protocol) ブリッジ。

> [stack-chan プロジェクト](https://github.com/stack-chan/stack-chan)（ししかわ／石川真也 さんが 2021 年に公開）のコミュニティから生まれ、M5Stack 公式が製品化した StackChan キットを対象としています。

```
┌─────────────┐     stdio MCP      ┌──────────────┐    WebSocket MCP    ┌──────────────┐
│ MCP client  │ ─────────────────▶ │   gateway    │ ──────────────────▶ │ ESP32 (CoreS3│
│ (Claude等)  │ ◀───────────────── │  (Python)    │ ◀────────────────── │  +StackChan) │
└─────────────┘                    │              │                     └──────────────┘
                                   │  /capture    │ ◀── HTTP POST (JPEG) ──┘
                                   └──────────────┘
```

任意の MCP クライアント (Claude Code / Claude Desktop / 他) から、首振り・カメラ撮影・タッチセンサ・アバター表情切替などの StackChan 操作を呼び出せる。

## 構成

このリポジトリはモノレポ。

| ディレクトリ | 内容 |
|---|---|
| `firmware/` | [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) フォーク全体（git subtree）。StackChan 用カスタムボードは `firmware/main/boards/stackchan/` に配置 |
| `gateway/` | Python MCP ゲートウェイ。stdio MCP サーバー (LLM側) + WebSocket MCP クライアント (ESP32側) + HTTP capture サーバー |
| `docs/` | [`architecture.md`](docs/architecture.md): 全体構成図・ツール名マッピング・写真フロー・認証・Phase ロードマップ。[`firmware-sync.md`](docs/firmware-sync.md): upstream xiaozhi-esp32 同期手順。[`remote-access.md`](docs/remote-access.md): Tailscale Funnel による非LAN接続手順 |
| `examples/` | オプションの非保守 example 群。[`cloudflare-relay/`](examples/cloudflare-relay/): LAN 外から gateway へ届くための Cloudflare Workers WebSocket リレー |

## 想定ハードウェア

**M5Stack 公式 [StackChan キット](https://docs.m5stack.com/ja/StackChan)** (Kickstarter 2025 出荷版)。公式ドキュメントの[出荷時ファームウェア](https://docs.m5stack.com/ja/StackChan#%E5%87%BA%E8%8D%B7%E6%99%82%E3%83%95%E3%82%A1%E3%83%BC%E3%83%A0%E3%82%A6%E3%82%A7%E3%82%A2)を本リポジトリの firmware で置き換える形で動作します。

| 部品 | 仕様 |
|---|---|
| **本体** | M5Stack CoreS3 (ESP32-S3, 16MB Flash, 8MB PSRAM) |
| **首サーボ** | SCS0009 ×2 (yaw + pitch、シリアルバス、TX=GPIO6, RX=GPIO7) |
| **カメラ** | GC0308 (DVP, 320×240) |
| **タッチ** | FT6336 / Si12T |
| **ディスプレイ** | ILI9342 (SPI, 320×240) |

> 自作の stack-chan（[stack-chan プロジェクト](https://github.com/stack-chan/stack-chan)のオリジナル設計に準拠）でも、上記のピンアサイン・I2C アドレスが一致していれば動く可能性があります。動作報告・修正 PR 歓迎です。

## ツール一覧 (gateway 経由で MCP クライアントが呼べる)

| ツール | 説明 | 状態 |
|---|---|---|
| `get_status` | ゲートウェイ接続状態 | ✅ |
| `get_device_info` | ESP32 デバイス状態 (バッテリー/音量/WiFi 等) | ✅ |
| `take_photo(question?)` | カメラ撮影 → JPEG 保存 → パス返す | ✅ |
| `set_volume(volume)` | スピーカー音量 (0-100) | ✅ |
| `set_brightness(brightness)` | 画面明るさ (0-100) | ✅ |
| `move_head(yaw, pitch, speed?)` | 首を動かす (サーボ)。`pitch` は M5Stack 推奨運用レンジ `5..85` に制限される。ファームウェア側のハードクランプ (`0..88`) を使いたい場合は、firmware-side の `set_head_angles` デバイスツールを利用する | ✅ |
| `get_touch_state` | タッチセンサ状態 (press/release/stroke 等) | ✅ |
| `set_avatar(face)` | アバター表情切替 (`idle` / `happy` / `thinking` / `sad` / `surprised` / `embarrassed`)、または `off` でアバターを隠し blink も停止して下層の WiFi 設定 / OTA / 設定画面を露出。他 face を指定するとアバター + blink が復帰 | ✅ |
| `set_blink(state)` | 瞬き ON/OFF | ✅ |
| `set_mouth(state)` | 口開閉（one-shot、次の呼び出しまで保持） | ✅ |
| `set_mouth_sequence(steps)` | TTS リップシンク用に `{shape, duration_ms}` のリストをデバイス側でキュー再生（ステップごとの WebSocket RTT ゆらぎなし） | ✅ |
| `check_vm_en` | サーボ電源 (VM EN HIGH) 状態確認 | ✅ |
| `set_led(index, r, g, b)` | ベース部の RGB LED 12 個のうち 1 個を指定 (index `0..11`、各チャネル `0..255`) | ✅ |
| `set_all_leds(r, g, b)` | ベース部の RGB LED 12 個すべてを同じ色に設定 | ✅ |
| `set_leds(colors)` | `[[r,g,b], ...]` 配列で先頭 N 個を一括設定（I2C 1 回のバースト送信、アニメーション等向け）。指定外の LED は前の色を保持 | ✅ |
| `clear_leds` | ベース部の RGB LED 12 個すべて消灯 | ✅ |
| `say(text, voice?, speaker_id?, reference_audio?)` | gateway 側 TTS でデバイススピーカーから喋らせる。本文内の対応 emoji で、同じ呼び出し内にアバター表情も切り替え可能。デフォルトエンジンは **VOICEVOX**（別 HTTP サービスとして起動 — [TTS セットアップ](#4-オプション-tts-セットアップ-voicevox) 参照）。`[tts]` extras が必要 | ✅ |
| `listen(duration_ms?, engine?, language?, model?, motion?, look_up_pitch?)` | デバイスマイクから短い発話をキャプチャし、gateway 側 STT で文字起こし。デフォルトエンジンは **faster-whisper**（ローカル動作・MIT — [STT セットアップ](#5-オプション-stt-セットアップ-faster-whisper) 参照）。任意の `motion` feedback で、キャプチャ中に `thinking` face を出したり、頭を上向きに傾けたりできます。`[stt-faster-whisper]`（または `[stt-openai]`）extras と、`listen` ワイヤタイプを受け付けるファームウェアが必要 | ✅ |

詳細スキーマは `gateway/README.md` 参照。

## クイックスタート

### 1. ファームウェア書き込み (CoreS3)

書き込み方法は 2 通り。エンドユーザーには **オプション A**（事前ビルド済みバイナリ）が手早く、ツールチェーンのセットアップ不要。コントリビュータがソースからビルドしたい場合は **オプション B**。

#### オプション A: 事前ビルド済みバイナリを焼く（エンドユーザー向け、推奨）

[Releases ページ](https://github.com/kisaragi-mochi/stackchan-mcp/releases) から最新の `firmware-v*` リリースを開き、`merged-binary.bin`（必要なら `xiaozhi.bin` も）をダウンロード。あとは `esptool.py` で焼くだけ:

```bash
# --port は使っている OS のシリアルデバイス名に置き換えてください:
#   macOS:   /dev/cu.usbmodem* (例: /dev/cu.usbmodem1101)
#   Linux:   /dev/ttyUSB0 または /dev/ttyACM0
#   Windows: COM3 (デバイスマネージャに表示されるポート名)

# 新規インストール（NVS が消えるので Wi-Fi 設定はやり直し）:
esptool.py --chip esp32s3 --port /dev/cu.usbmodem1101 -b 460800 \
  write_flash 0x0 merged-binary.bin

# アプリだけ更新（NVS 保持 — Wi-Fi 設定そのまま）:
esptool.py --chip esp32s3 --port /dev/cu.usbmodem1101 -b 460800 \
  write_flash 0x20000 xiaozhi.bin
```

ESP-IDF や Docker のセットアップは不要。

#### オプション B: ソースから Docker でビルド（コントリビュータ向け）

このリポジトリは `firmware/components/` 配下に git submodule を使っています。
`--recursive` を付けずに clone した場合は、先に初期化してください:

```bash
git submodule update --init --recursive
```

その後ビルド:

```bash
cd firmware
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v $PWD:/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
# → releases/v2.2.6_stackchan.zip

# フラッシュ (CoreS3 を USB 接続後)
# --port は使っている OS のシリアルデバイス名に置き換えてください
# — オプション A の表（macOS/Linux/Windows）を参照
esptool.py --chip esp32s3 --port /dev/cu.usbmodem1101 -b 460800 \
  write_flash 0x0 build/merged-binary.bin
```

`--cpus=4` フラグは Docker コンテナの並列度をキャップして、LVGL や
`xiaozhi-fonts/emoji_*.c` のコンパイル時の同時 `gcc` 数を抑えるためのものです。これがないと `ninja` が `/proc/cpuinfo` から CPU 数を
自動検出し、その結果並列に動く `gcc` がコンテナのメモリを使い切って、
物理 RAM に余裕があるホストでも LVGL の途中で `Cannot allocate memory`
で build が落ちることがあります（#112 で追跡）。`--ulimit
nofile=65536:65536` フラグは別の問題で、同じ LVGL emoji コンパイル時に
デフォルトのファイルディスクリプタ上限下で発生する `Too many open
files` エラーを回避します。Linux ホストではデフォルトが十分高いため
影響ありませんが、両フラグを無条件に付けて問題なく、CI とも揃います。

書き込み後、WiFi 設定は ESP32 が起動してから行う — スマホで設定 UI に接続（xiaozhi-esp32 標準フロー）。

ローカルネットワーク上では、ゲートウェイは既定で
`_stackchan-mcp._tcp.local.` を mDNS/DNS-SD で広告します。primary URL が
まだ保存されていない新規ファームウェアは、この情報から WebSocket
endpoint を自動検出できます。mDNS discovery には NVS `websocket.url` が空である必要があります。

### WebSocket gateway URL と認証トークンの設定

primary URL の解決順序:

1. NVS `websocket.url`
2. `CONFIG_STACKCHAN_MDNS_DISCOVERY` が有効で、primary NVS URL が空のときの mDNS `_stackchan-mcp._tcp.local.`
3. `CONFIG_DEFAULT_WEBSOCKET_URL`
4. 空のままなら boot log にエラーを出して失敗

既存の `websocket.fallback_url` と
`CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL` の候補は、上記 primary candidate
path の後で引き続き試行されます。`CONFIG_FORCE_DEFAULT_WEBSOCKET_URL=y`
は古い NVS からの復旧用の明示的な例外として残り、非空の Kconfig URL
が優先される場合は mDNS discovery をスキップします。

ゲートウェイは既定で mDNS を広告します。広告を止めるには
`stackchan-mcp --no-mdns` を使います。ファームウェア側の discovery を
compile out するには `CONFIG_STACKCHAN_MDNS_DISCOVERY=n` を設定します。
discovery にはローカル LAN 上の UDP multicast が必要で、router や VLAN
によっては遮断されます。複数のゲートウェイが見える場合、ファームウェア
は 1 回の browse で見つかったすべての supported gateway service について
usable IPv4 address をそれぞれ試し、accepted instance 数と candidate
address list を log に出します。mDNS が見つけるのは URL だけで、認証は引き続き
`websocket.token` / `CONFIG_DEFAULT_WEBSOCKET_TOKEN` が制御します。

LAN 上では mDNS で自動検出し、LAN 外では relay に自動 fallback したい場合は、NVS
`websocket.url` を空にして、relay URL を `websocket.fallback_url` に保存します。この構成では mDNS candidate が先に試され、local discovery path が WebSocket server hello まで完了しなかった場合に relay が後で試されます。

gateway host IPv4 が変わった後の自動復旧には、paired mDNS fix の両側が必要です:
Gateway vA.B.C+ は host address 変更時に advertised service を更新し、
Firmware vX.Y.Z+ は 1 回の browse で見つかったすべての supported mDNS service
instance を試します。それより古い firmware は、再起動するまで stale cache 上の
instance を試し続ける場合があります。

ファームウェアはゲートウェイ接続のために以下の NVS キーを参照します:

- `websocket.url` — ゲートウェイ WebSocket URL (例: `ws://<gateway-host>:8765/`)
- `websocket.fallback_url` — `websocket.url` に接続できない、または server hello が完了しない場合に試す 2 番目の gateway URL
- `websocket.token` — `Authorization: Bearer <token>` で送信される bearer トークン。ゲートウェイ側の `STACKCHAN_TOKEN` / `BEARER_TOKEN` と照合される (両方空にすれば認証スキップ)

設定方法は実用的に 4 つあり、加えて一時的な source-level escape hatch があります:

1. **Kconfig によるビルド時デフォルト (開発者推奨)**: `idf.py menuconfig` → `Component config` → `Xiaozhi Assistant` を開き、以下を設定:
   - `Default WebSocket gateway URL (fallback when NVS is empty)` →
     `CONFIG_DEFAULT_WEBSOCKET_URL` (例: `ws://<gateway-host>:8765/`)
   - `Fallback WebSocket gateway URL` →
     `CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL`
   - `Default WebSocket auth token (fallback when NVS is empty)` →
     `CONFIG_DEFAULT_WEBSOCKET_TOKEN` (ゲートウェイが認証不要なら空のままで OK)

   デフォルトでは、対応する NVS キーが空のときだけこの値が使われます。新規デバイスへの初回フラッシュではちょうど期待通りに動作します。primary と fallback の両方を設定した場合、ファームウェアは決まった順番で候補を試し、WebSocket の server hello まで完了した最初の候補を使います。

2. **デバイス上の WiFi 設定 UI を使う（新規ユーザー向け推奨）**: デバイスが WiFi 設定モードになっているとき、`http://192.168.4.1` のキャプティブポータルを開き、**Advanced** タブに切り替えて以下を入力します:
   - **WebSocket Gateway URL**（例: `ws://<gateway-host>:8765/`） — primary な gateway 候補。
   - **Fallback Gateway URL**（例: `wss://<relay-host>/`） — 任意の 2 番目の候補。primary が server hello 完了に失敗したときだけ試行されます。
   - **Gateway Token** — 任意の bearer トークン。設定時は両候補に対して `Authorization: Bearer <token>` ヘッダで送信されます。WiFi 設定 UI の AP は未認証で開かれるため、GET エンドポイントはトークンの有無だけを返し、現在の値は表示されません。空のまま送信すると既存トークンが保持され、新しい値を入力すると更新、❌ ボタンで build-time の `CONFIG_DEFAULT_WEBSOCKET_TOKEN` に戻ります。Kconfig 既定値が未設定のビルドでは ❌ が認証なしを意味しますが、既定値が組み込まれているビルドでは ❌ で実際に認証が解除されるわけではなく、その既定値に戻る点に注意してください。組み込み既定値があるビルドで認証なしの gateway に向けたい場合は、Kconfig 既定値を空にして再ビルドするか、gateway 側の token を build-time 既定値に揃えてください。

   送信すると値が `websocket` NVS namespace（`websocket.url` / `websocket.fallback_url` / `websocket.token`）に永続化され、次回起動時に読み込まれます。pre-built ファームウェアを使うエンドユーザー向けの想定経路です。URL フィールド横の ❌ ボタンでクリアしてから再度送信すると、対応する `CONFIG_DEFAULT_WEBSOCKET_*` Kconfig 値（Kconfig 既定値が未設定なら「fallback なし」）に戻ります。

3. **接続済み状態で runtime MCP tool を使う**: `gateway_config_get` は `websocket.url`、`websocket.fallback_url`、token が設定済みかどうか、`force_mode`、`discovery_enabled`、現在接続している candidate URL を返します。token の値そのものは返しません。`gateway_config_set` は任意の `url`、`fallback_url`、`token` 文字列を受け取ります。空文字を渡すとその NVS key を clear します。たとえば `url=""` で primary NVS URL を clear すると、次回 reconnect で mDNS discovery path が復帰します。変更はすぐ永続化されますが、disconnect / reconnect / reboot は発火しません。

4. **NVS に直接 `websocket.url` / `websocket.fallback_url` / `websocket.token` を書き込む（上級者向け）**: 例えば独自の NVS 書き込みツールをシリアル経由で使うケース。WiFi 設定 UI や runtime MCP tool と同じ永続化セマンティクス。バッチ provisioning などで主に使います。

5. **一時的なソース hardcode (非推奨)**: `websocket_protocol.cc` を編集すればローカル実験はアンブロックできますが、commit には残さないようにしてください。

よく使う gateway URL 構成:

| モード | Primary URL | Fallback URL |
| --- | --- | --- |
| LAN 自動検出 + relay fallback | 空 (mDNS) | `wss://<relay-host>/` |
| 固定ローカルのみ | `ws://<gateway-host>:8765/` | 空 |
| relay のみ | `wss://<relay-host>/` | 空 |
| 固定ローカル優先 + relay fallback | `ws://<gateway-host>:8765/` | `wss://<relay-host>/` |

#### 既存デバイス (古い NVS) — `CONFIG_FORCE_DEFAULT_WEBSOCKET_URL`

以前に上流の xiaozhi-esp32 ファームウェアが書き込まれたことがあるデバイスにフラッシュする場合、NVS には上流の OTA-config パスが書き込んだ `websocket.url=wss://api.tenclass.net/...` が既に存在します。この場合、上記オプション 1 の empty-NVS fallback は **発動せず**、デバイスはローカルゲートウェイではなく tenclass を呼び続けます。

デバイスがどれかの candidate で stackchan-mcp へ接続できる場合は、`gateway_config_set` に `url=""` を渡して古い primary NVS URL を clear すると、次回 reconnect で mDNS discovery path が復帰します。古い URL のせいで stackchan-mcp への接続自体が成立しない場合は、NVS を全消去 (WiFi 認証も飛ぶ) せずに回避するため、force-override スイッチを有効化します:

- `Force CONFIG_DEFAULT_WEBSOCKET_URL/TOKEN to override NVS` →
  `CONFIG_FORCE_DEFAULT_WEBSOCKET_URL=y`

有効化すると、**非空** の Kconfig URL/トークンが NVS の値を上書きします。空の Kconfig 値は引き続き NVS にフォールバックするため、たとえばトークン用 Kconfig を空のままにすれば NVS に保存されたトークンがそのまま使われ続けます。boot ログに `FORCE: overriding NVS websocket.url with Kconfig: NVS=... -> ...` と出るので、override が効いたことを確認できます。このスイッチは、xiaozhi 出身のハードウェアをローカル stackchan-mcp ゲートウェイに引き寄せる、または CI/dev イメージを既知のゲートウェイ URL に固定するための、最も推奨される手段です。

スイッチは opt-in なので、ランタイムで設定するエンドユーザーデバイスは NVS-priority のままです。

#### 開発者ローカル設定 — `sdkconfig.defaults.local`

ローカル実機テスト用の個人 gateway URL や token は、追跡対象の
`firmware/sdkconfig.defaults` には入れないでください。代わりに gitignore
済みのローカルファイルを作ります:

```bash
cd firmware
cat > sdkconfig.defaults.local <<'EOF'
CONFIG_DEFAULT_WEBSOCKET_URL="ws://<your-lan-ip>:8765/"
CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL="wss://<relay-host>/"
CONFIG_DEFAULT_WEBSOCKET_TOKEN="<your-dev-token>"
CONFIG_FORCE_DEFAULT_WEBSOCKET_URL=y
EOF
```

このファイルが存在する場合、`python ./scripts/release.py <board>` と通常の
`idf.py build` の両方で読み込まれます。gitignore 済みなので、`git add -A`
で個人設定を誤って追加する事故を防げます。

### 2. ゲートウェイ起動

ゲートウェイは PyPI で公開されているパッケージをインストールする方法
(エンドユーザー向け) と、このリポジトリのチェックアウトから動かす方法
(`main` を追いたいコントリビュータ向け) のどちらでも使えます。

#### オプション A: ツールとしてインストール (エンドユーザー向け、推奨)

システム Python や他の Python プロジェクトと衝突しない、隔離された
インストールを行うには、以下のいずれかを使います:

```bash
uv tool install stackchan-mcp
# または
pipx install stackchan-mcp
```

ゲートウェイ起動:

```bash
stackchan-mcp
```

プロジェクト管理の virtualenv で動かしたい場合は、有効化した venv の中で
`pip install stackchan-mcp` でも動きます。その venv 内では
`python -m stackchan_mcp` も `stackchan-mcp` と同等です。ただしシステム
Python に対して直接 `pip install` するのは避けてください (PEP 668)。

[`gateway/README.md`](gateway/README.md#setup) に記載されている
`STACKCHAN_TOKEN` / `VISION_HOST` 等の設定値は、環境変数・シェル・
カレントディレクトリの `.env` ファイルのいずれからでも渡せます。

#### オプション B: ソースから uv で起動 (コントリビュータ向け)

```bash
cd gateway
cp .env.example .env       # STACKCHAN_TOKEN / VISION_HOST を設定
uv sync
uv run python -m stackchan_mcp
```

ESP32 接続中にゲートウェイを再起動した場合、ファームウェアは idle 中に
WebSocket 接続を自動再試行します。再試行間隔は 5 秒から始まり、最大 60 秒
まで伸びます。デバイスが戻ったかどうかは `get_status` で確認できます。
ハンドシェイク後にゲートウェイ側からセッションが切られた場合 (gateway クラッシュ、
TLS レイヤの切断、ハンドシェイク後にセッションを閉じる構成のゲートウェイなど) でも
同じ再試行経路が動くので、次の接続試行をゲートウェイが受け入れた時点で
自動復帰します。

別ネットワークから使う場合は、Tailscale Funnel と `VISION_URL` による capture
callback 設定を [`docs/remote-access.md`](docs/remote-access.md) にまとめています。

### 3. MCP クライアント登録 (Claude Code 例)

`~/.claude.json` に追加します。

`pip install stackchan-mcp` でインストールした場合:

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "stackchan-mcp",
      "env": {
        "STACKCHAN_TOKEN": "your-secret-token-here",
        "VISION_HOST": "your.host.lan.ip"
      }
    }
  }
}
```

ソースから `uv` で動かす場合:

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/stackchan-mcp/gateway",
        "python", "-m", "stackchan_mcp"
      ]
    }
  }
}
```

詳細は `gateway/README.md` 参照。

### 4. オプション: TTS セットアップ (VOICEVOX)

デバイスを喋らせるには、`[tts]` extras をインストールして
[VOICEVOX](https://voicevox.hiroshiba.jp/) エンジンを gateway と
並行起動します。VOICEVOX は別 HTTP プロセスとして動くので、
LGPL-3.0 ライセンスはそのプロセスの中だけで完結し、MIT
ライセンスの gateway は HTTP リクエストを発行するだけです。

#### エンジン起動 (Docker)

```bash
docker run --rm -p '127.0.0.1:50021:50021' \
  voicevox/voicevox_engine:cpu-ubuntu20.04-latest
```

デフォルトポートは 50021。短い発話なら CPU 版で十分。GPU 版も
upstream に公開されています。

#### TTS extras のインストール

```bash
pip install 'stackchan-mcp[tts]'
# または等価:
pip install 'stackchan-mcp[tts-voicevox]'
```

これで `httpx` (HTTP クライアント) と `opuslib` (Opus
エンコーダバインディング) が入ります。エンコーダはシステム側に
`libopus` が必要 — macOS なら `brew install opus`、Debian/Ubuntu
なら `sudo apt-get install libopus0`。

#### 設定 (任意)

| 環境変数 | デフォルト | 補足 |
|---|---|---|
| `STACKCHAN_VOICEVOX_URL` | `http://127.0.0.1:50021` | VOICEVOX エンジンの URL |
| `STACKCHAN_VOICEVOX_DEFAULT_SPEAKER` | `3` | デフォルト話者 ID（ずんだもん ノーマル）。他の話者は [VOICEVOX 公式](https://github.com/VOICEVOX/voicevox_engine) 参照 |

#### 試す

MCP クライアントから:

```
say(text="こんにちは、わたしはスタックチャンです")
```

gateway は VOICEVOX に POST → 返ってきた WAV をデコード →
16 kHz mono にリサンプル → 60 ms の Opus フレームにエンコード →
既存の WebSocket バイナリチャネルでデバイスへ送信、という流れで
喋らせます。デバイスは受け取ったフレームを既存の音声デコーダで
再生するだけなので、**ファームウェア側の変更は不要**です。
TTS フレームワークはエンジン非依存なので、他のエンジンも同じ
`say` API の裏に差し込めます — 下記の Irodori エンジンを参照。

`say` の本文には対応する表情 emoji も入れられます: happy
(😊 😄 😀 😁 🙂 😆 🥰 😍 😋 🤗), sad (😢 😭 😞 😔 ☹️ 🙁 😿),
surprised (😲 😮 😯 😱 🤯), embarrassed (😳 😅 🫣), thinking
(🤔 🧐 💭)。最初に見つかった対応 emoji が、同じ MCP 呼び出し内で
発話前にアバター表情を切り替えます。未対応 emoji は表情を変えません。
VOICEVOX など emoji-style 非対応エンジンでは、合成前にすべての emoji を
取り除きます。取り除いた結果が空文字列になる場合は、表情変更だけを試み、
発話は skipped として返します。

#### 別エンジン: Irodori

Irodori は MP3 を返す外部合成サービスを呼ぶ 2 つ目の TTS
エンジンです。VOICEVOX と同じ `say` パイプラインに乗り（MP3 を
16 kHz mono PCM にデコードしてから Opus にエンコード）、入力
テキストに含まれる emoji をそのまま受け取って voice-style cue として
扱えます。

**デフォルトのエンドポイントはありません** — 合成バックエンドは
ホスト型サービスであり、URL をハードコードすると全インストールが
他人のデプロイを指してしまうためです。互換性のある合成 API を
自分でホストし（例: 参照元の Hugging Face Space を複製する、または
同じリクエスト/レスポンス契約に従う独自サービスを動かす）、
gateway をそこに向けてください。

extras のインストール:

```bash
pip install 'stackchan-mcp[tts-irodori]'
```

これで `httpx`・`opuslib`・`miniaudio`（プレビルト wheel 付きの
小さな自己完結型 MP3/音声デコーダ。`opuslib` が要求する `libopus`
以外に追加のシステムライブラリは不要）が入ります。

環境変数で設定します（URL とキーは環境変数からのみ読み込み —
コミットしないでください）:

| 環境変数 | デフォルト | 補足 |
|---|---|---|
| `STACKCHAN_IRODORI_URL` | _(必須)_ | 自前ホストした合成サービスのエンドポイント URL。未設定 → エンジンは一覧に出るが `say(voice="irodori")` は明確なエラーを返す |
| `STACKCHAN_IRODORI_KEY` | _(なし)_ | 任意の API キー。クエリパラメータとして送信 |
| `STACKCHAN_IRODORI_SPEAKER` | `3` | デフォルト話者 ID |
| `STACKCHAN_IRODORI_STEPS` | `24` | デフォルト拡散ステップ数（大きいほど遅く高品質） |

呼び出しごとに Irodori を選択:

```
say(text="やったね😊", voice="irodori")
```

または `voice` を省略した全 `say` 呼び出しのデフォルトエンジンに:

```bash
export STACKCHAN_TTS_ENGINE=irodori
```

`STACKCHAN_TTS_ENGINE` 未設定なら VOICEVOX がデフォルトのままで、
明示的な `voice` 引数は常にデフォルトを上書きします。

### 5. オプション: STT セットアップ (faster-whisper)

デバイスに聞いてもらうには、`[stt-*]` extras のどれかをインストール
し、`listen` ワイヤタイプを受け付けるファームウェア（本リリース以降）
とペアにします。gateway が `listen.start` 通知を投げると
ファームウェアがマイクを開き、上り Opus フレームが gateway で
デコード・文字起こしされます — デフォルトの `faster-whisper`
エンジンを使う限り、音声がマシンの外に出ることはありません。

#### STT extras のインストール

ローカル文字起こし（[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、
MIT ライセンス、CTranslate2 ベース、CPU で動作）:

```bash
pip install 'stackchan-mcp[stt-faster-whisper]'
```

[OpenAI Whisper API](https://platform.openai.com/docs/guides/speech-to-text)
（クラウド、ローカル計算資源が少ない場合に有用）:

```bash
pip install 'stackchan-mcp[stt-openai]'
export OPENAI_API_KEY=sk-...
```

どちらの extras にも、上り Opus フレームのデコードに必要な
`opuslib` を含む `[stt]` ベース extras が含まれます。デコーダは
システム側の `libopus` ライブラリを要求します — macOS なら
`brew install opus`、Debian/Ubuntu なら
`sudo apt-get install libopus0`（`[tts]` extras と同じ前提）。

#### 設定 (任意)

| 環境変数 | デフォルト | 補足 |
|---|---|---|
| `STACKCHAN_FASTER_WHISPER_MODEL` | `base` | モデル識別子 — `tiny` / `base` / `small` / `medium` / `large-v3`。大きいモデルほど精度は上がるが、メモリ消費と推論時間も増える |
| `STACKCHAN_FASTER_WHISPER_DEVICE` | `cpu` | `cpu` / `cuda` / `auto` |
| `STACKCHAN_FASTER_WHISPER_COMPUTE_TYPE` | `int8` | `int8` / `float16` / `float32` |
| `STACKCHAN_OPENAI_WHISPER_MODEL` | `whisper-1` | OpenAI Whisper モデル識別子（公式 API では現状 `whisper-1` のみ） |

#### 試す

MCP クライアントから:

```
listen(duration_ms=5000, language="ja")
```

gateway がデバイスに `{"type":"listen","state":"start","mode":"manual"}`
を送信 → キャプチャ窓の間に上ってくる Opus フレームをバッファ →
`{"type":"listen","state":"stop"}` を送信 → 蓄積した音声を登録済み
STT エンジンに渡して文字起こし、という流れです。`faster-whisper`
エンジンの初回呼び出しではモデル（`base` で約 140 MB）が Hugging
Face キャッシュにダウンロードされ、以降は再利用されます。
見た目でキャプチャ中であることを示したい場合は、`motion="face-only"`
でキャプチャ中に `thinking` アバターを表示して終了時に `idle` へ戻すか、
`motion="look-up"` で yaw を保ったまま pitch を `look_up_pitch`
（デフォルト 50°、有効範囲 5..85°）へ傾け、`thinking` を表示し、
成功時はその姿勢を保持できます。STT フレームワークもエンジン非依存なので、
Vosk・whisper.cpp・他のクラウドサービス等を `listen` API を変えずに
後から追加できます。

### 6. オプション: イベント通知の有効化

Stack-chan の物理イベント（現在対応: touch tap / stroke、構造上は
将来の subtype 追加が可能）は、3 つの通知 channel で配信できます。
すべての channel はデフォルトで無効です。
`~/.config/stackchan-mcp/notify.yml` で opt-in してください。複数の
channel を同時に有効化することもでき、その場合は有効化された各
channel に同じイベントが配信されます。連携するホストに合わせて
選んでください。

- `channels` — Claude Code plugin 経路。Claude Code の実験的な
  Channels capability 経由で、`<channel ...>` ブロックとして
  セッションに inject されます。Stack-chan を Claude Code plugin
  として install し、イベントを会話の中に届けたい場合に使います。
- `jsonl` — プロセス外ファイル連携。各イベントが 1 行の JSON として
  指定したファイルに追記されます。Claude Code 以外のホストや独自
  パイプラインが、ファイルを tail して非同期に取り込みたい場合に
  使います。
- `legacy_event` — plugin 以前の MCP notification。gateway が独自
  定義の `stackchan/event` MCP notification method を送出します。
  Channels capability 以前から存在する経路です。ホストが Claude Code
  plugin 経路ではなく `~/.claude.json` `mcpServers` で gateway を
  起動している場合の backward compatibility 用です。

詳細な注釈付き設定リファレンスは `notify.example.yml` を参照して
ください。

#### Channel: `channels`（Claude Code plugin 経路）

Stack-chan を Claude Code plugin として起動し、セッション内に channel
ブロックとしてイベントを届けたい場合に使います。この channel は
変化する可能性のある実験的な MCP capability を使います。

`notify.yml` で有効化:

```yaml
channels:
  enabled: true
```

Claude Code session に `<channel ...>` ブロックが inject されます:

```
<channel source="plugin:stackchanmcp:stackchanmcp" ...>head was tapped</channel>
```

セットアップ:

1. Plugin install — `kisaragi-mochi-channels` marketplace から、本
   リポジトリを Claude Code plugin として install します。

   ```bash
   claude plugin install stackchanmcp@kisaragi-mochi-channels
   ```

   ローカル開発で作業コピーを使う場合は、marketplace install の
   代わりに `--plugin-dir /path/to/stackchan-mcp` で本リポジトリを
   指してください。Claude Code は同梱の `.mcp.json` 経由で
   `${CLAUDE_PLUGIN_ROOT}/gateway` 配下の gateway を起動します。

2. ホスト環境設定 — Channels 経路では、3 箇所のホスト側名前を
   gateway の MCP server 名（`stackchanmcp`、ハイフン無し）と
   揃える必要があります。

   - Plugin の `.mcp.json` `mcpServers` key を `stackchanmcp` にする。
     以前別の key で gateway を wire していた場合は rename して
     ください。
   - Claude Code の `settings.local.json` の `enabledMcpjsonServers`
     whitelist に `stackchanmcp` を含める。
   - Channels allowlist は system-wide 承認が必要です。Claude Code は
     user-level（`~/.claude/settings.json` 等）の設定は Channels
     allowlist として有効になりません。macOS では
     `/Library/Application Support/ClaudeCode/managed-settings.json` を
     作成または編集してください（`sudo` 必要）。

     ```json
     {
       "channelsEnabled": true,
       "allowedChannelPlugins": ["stackchanmcp@kisaragi-mochi-channels"]
     }
     ```

3. 受け口側 — Channels フラグつきで Claude Code を起動します。

   ```bash
   claude --channels plugin:stackchanmcp@kisaragi-mochi-channels \
          --dangerously-load-development-channels plugin:stackchanmcp@kisaragi-mochi-channels
   ```

   `--channels` フラグは channel source を gateway に attach し、
   session に `<channel source="plugin:stackchanmcp:stackchanmcp" ...>`
   blocks を inject します。`--dangerously-load-development-channels`
   フラグは現状 `--channels` と併用が必要です。plugin の Channels
   capability が実験的で、approved-allowlist のみ経路（このフラグ
   なし）では現行 Claude Code 版で notification が届かないことが
   検証されています。Channels capability が安定化したら、このフラグは
   optional になる予定です。

重要 — plugin 以前の起動経路では Channels が届きません: 以前
`~/.claude.json` の `mcpServers` 経由でこの gateway を起動して
いた場合、その旧経路では `<channel ...>` の inject は届きません。
Claude Code は plugin 経由で起動された MCP server のみに channel
source を付ける仕様です。plugin 経路に移行する前に、既存の gateway
プロセスを停止して ESP32 ownership lock を解放してください。そう
しないと plugin 経由で起動した gateway が lock 取得に失敗します。
`~/.claude.json` 経路のまま使いたい場合は、下記の `legacy_event` /
`jsonl` channel を使ってください。どちらも plugin loading なしで
動作します。

他ホスト:

- `claude/channel` 互換の受け口を持つ他ホスト: 当該ホストの
  ドキュメントに従って受け口を開いてください。Claude Code 以外の
  ホストとの互換性は当リポジトリでは未検証です。
- Channels 受け口を持たないホスト: 下記の `jsonl` channel を
  使ってください。

##### 旧 `stackchan-mcp`（ハイフン入り）form からの migration

以前 `stackchan-mcp` server name form で Channels を有効化していた
場合、ホスト MCP client と gateway を揃えるため、以下を現行の
`stackchanmcp` form（ハイフン無し）に rename してください。

- Plugin / server 名: ホストの `.mcp.json` `mcpServers` key、
  `settings.local.json` `enabledMcpjsonServers` whitelist、および
  `--channels` / `--dangerously-load-development-channels` フラグの
  引数。`stackchan-mcp`（ハイフン入り）を `stackchanmcp` に変更。
- Channels フラグ form: `--channels server:stackchan-mcp` を
  `--channels plugin:stackchanmcp@kisaragi-mochi-channels` に変更。
  marketplace manifest が公開済のため、plugin form が現行の正式 form
  です。
- system-wide allowlist:
  `/Library/Application Support/ClaudeCode/managed-settings.json` の
  `allowedChannelPlugins` に `stackchanmcp@kisaragi-mochi-channels`
  （旧 `stackchan-mcp` form ではない）を列挙してください。

これらすべての rename がなければ、ホスト MCP client は
`Channel notifications skipped: server <name> not in --channels list
for this session` を log し、notification が session に届きません。

#### Channel: `jsonl`（プロセス外ファイル連携）

Claude Code 以外のホストや独自パイプラインが、ファイルを tail して
イベントを非同期に取り込みたい場合に使います。Claude Code 以外の
任意の連携先に対して最も簡単に組み込める channel です。

`notify.yml` で有効化:

```yaml
jsonl:
  enabled: true
  path: ~/.claude/stackchan-events.jsonl
```

各イベントは、設定したパスに 1 行の JSON として追記されます。
ファイルが存在しない場合は作成され、既存のエントリは保持されます。
1 行に含まれる field:

- `event_type` — top-level event type（現状は `"touch"`）。
- `subtype` — event type 内の subtype（現状は `"tap"` または
  `"stroke"`）。
- `duration_ms` — firmware が報告したイベントの継続時間（ミリ秒）。
- `ts` — firmware uptime（ミリ秒、monotonic）。
- `ts_unix` — gateway がイベントを記録した壁時計時刻。
- `session_id` — gateway session 識別子。
- `action` — event subtype に対応する avatar action keyword（例:
  `head_pat`, `head_stroke`）。組み込みの default template は常に
  この値を埋めるほか、`messages:` override 側でも `action` の指定が
  必須であるため、対応する全 subtype で必ず含まれます。

レンダリングされた文言（例: `head was tapped`）は `channels` channel
が人間可読のメッセージとして配信するもので、JSONL レコード自体には
保存されません。tap と stroke の配信例:

```json
{"event_type": "touch", "subtype": "tap", "duration_ms": 0, "ts": 123456, "ts_unix": 1717862400.0, "session_id": "abc-123", "action": "head_pat"}
{"event_type": "touch", "subtype": "stroke", "duration_ms": 720, "ts": 124000, "ts_unix": 1717862400.7, "session_id": "abc-123", "action": "head_stroke"}
```

最上位の `event_type` / `subtype` は下記「Supported event subtypes」
の行と対応します。

#### Channel: `legacy_event`（plugin 以前の backward compatibility）

ホストが Claude Code plugin 経路ではなく `~/.claude.json` `mcpServers`
で gateway を起動していて、ホストを plugin form に切り替えずに
イベントを受け取りたい場合に使います。

`notify.yml` で有効化:

```yaml
legacy_event:
  enabled: true
```

gateway は独自定義の `stackchan/event` MCP notification method を
送出します。notification params には上記 JSONL レコードと同じ field
が含まれます（ただし `ts_unix` は JSONL writer 側だけが付与するため
legacy notification には含まれません）。受信側の notification の
扱いはホスト依存です: Claude Code の plugin 以前経路では従来、
レンダリング後のメッセージが inline で表示されました。他ホストでは
異なる扱いになる場合があります。

#### Supported event subtypes

現在対応している物理イベントは下記の通りです。構造は意図的に拡張
可能で、`touch` の subtype 追加や新しい top-level type（例:
`motion`, `voice`）が後続の release で追加された場合も、本
セクションの全面書き直しなしに追記できます。

| Type | Subtype | デフォルト `action` | デフォルト `template` |
| --- | --- | --- | --- |
| `touch` | `tap` | `head_pat` | `head was tapped` |
| `touch` | `stroke` | `head_stroke` | `head was stroked for {duration_ms}ms` |

組み込みのデフォルトは「機械的なイベント名」ではなく「デバイスが
何を感じたか」を表す体験的な表現にしてあり、受信側のエージェントが
一人称のナレーションとして読めるようになっています。`{duration_ms}`
プレースホルダは event payload から置換され、未知のプレースホルダは
そのまま保持されます。

##### 文言の上書き

`~/.config/stackchan-mcp/notify.yml` に `messages:` block を追加
すると、subtype ごとの `action` / `template` を上書きできます。
記載した subtype だけが上書きされ、それ以外は上記のデフォルトの
ままです。

```yaml
# ~/.config/stackchan-mcp/notify.yml
messages:
  touch:
    tap:
      action: head_pat
      template: "got a head pat"
    stroke:
      action: head_stroke
      template: "head being stroked for {duration_ms}ms"
```

上書きする各 subtype には `action` と `template` の両方が必要です。
`action` の値はイベントの metadata に転送されるため、下流の consumer
がそれを key にしている場合は安定させておいてください。詳細な注釈
付きリファレンスは `notify.example.yml` を参照してください。

## アバター画像について

`firmware/main/boards/stackchan/avatar_images.cc` は **真っ黒 RGB565 のプレースホルダ** です。ビルドは通りますが、画面には何も表示されません。

個人用アバターを使う場合は、PNG 元画像を git の外に置き、git に無視されるローカル override ファイルを生成します。

```bash
cd firmware
python scripts/avatar_convert/convert_avatars.py
```

既定では、変換スクリプトは `~/.stackchan/avatar/` から PNG を読み込み、以下を書き出します。

- `firmware/main/boards/stackchan/avatar_images.local.cc`
- `firmware/main/boards/stackchan/avatar_images.local.h`

これらのローカルファイルは git に無視されます。`avatar_images.local.cc` が存在する場合、StackChan firmware のビルドでは追跡対象の黒いプレースホルダではなく、このローカルファイルが使われます。そのため、`git pull` で個人用アバターが上書きされません。

追跡対象の `avatar_images.cc` / `avatar_images.h` は公開Repo用のプレースホルダです。メンテナが意図してこれらを更新する場合だけ `--tracked` を指定できますが、個人用アバターでは既定のローカル出力先を使ってください。

すでに一度 firmware をビルドしたあとにローカルアバターを追加した場合は、CMake が新しい override を拾えるように `firmware/build/` を削除してから再ビルドしてください。

シンボル一覧 (`avatar_images.h` 参照):
- 表情系 (6): `avatar_idle`, `avatar_happy`, `avatar_thinking`, `avatar_sad`, `avatar_surprised`, `avatar_embarrassed`
- 目 (3): `avatar_eyes_open`, `avatar_eyes_half`, `avatar_eyes_closed`
- 口 (5): `avatar_mouth_closed`, `avatar_mouth_half`, `avatar_mouth_open`, `avatar_mouth_e`, `avatar_mouth_u`

`~/.stackchan/avatar/` に置く PNG ファイル名:

- 表情系: `idle.png`, `happy.png`, `thinking.png`, `sad.png`, `surprised.png`, `embarrassed.png`
- 目: `eyes_open.png`, `eyes_half.png`, `eyes_closed.png`
- 口: `mouth_closed.png`, `mouth_half.png`, `mouth_open.png`, `mouth_e.png`, `mouth_u.png`

個人用 PNG、生成済みローカルアバター、撮影画像、その他ユーザー固有のアセットは commit しないでください。

## 既知の課題

- 大角度急逆転 (±60° → -60° 等) でサーボハングする場合あり (Motion::update_task の補間移植で改善予定)
- タッチセンサ (Si12T) のぽん判定取りこぼし (感度レジスタ調整余地)

## ハードウェア安全上の注意

> ⚠️ **Y軸 (pitch) の安全範囲 — 2 層ガード**

Pitch 軸は firmware に組み込まれた 2 つの相補的なガードで保護されており、いずれも `set_head_angles` MCP ツールの description にも反映されています:

| 層 | 範囲 | 強制方法 | 根拠 |
|---|---|---|---|
| **Tier 1 — ハードクランプ** | `0..+88°` | 黙ってクランプ + `ESP_LOGW` | 機械的破損の防止。下限 `0°` は M5Stack CoreS3 + SCS0009 ハードウェアで実機検証したエンドストップ (`-1°` 付近) から ~1° のマージンを取った値 (PR #81)。上限 `88°` は Issue #98 の実機 sweep で `pitch=89°` に audible sub-stall（「じーーー」というギア負荷音）が観測されたため、そこから ~1° 内側に取った値。 |
| **Tier 2 — 推奨動作範囲** | `5..+85°` | 受け付ける + `ESP_LOGI` (ソフトシグナル) | M5Stack 公式が長期信頼性のために推奨している範囲。この外を 1 回叩いてもハード破損には至りませんが、`5..85°` の外で長期間動かし続けるとサーボに負担が蓄積する可能性があります。 |

M5Stack 公式ドキュメントには以下の警告があります:

> The movement angle of the StackChan Y-axis servo (vertical direction) is recommended to be controlled within 5 ~ 85°. Operating at extreme angles may cause **servo stall and permanent damage**.
> — https://docs.m5stack.com/en/StackChan ("Motion Angle Notice")
>
> (StackChan の Y 軸サーボの可動角度は 5°〜85° の範囲内に制御することを推奨します。極端な角度で動作させると **サーボストール や 永久故障** を引き起こす可能性があります。)

`set_head_angles` MCP ツールは pitch を完全に **permissive** な schema range として宣言しています — `int` 型の全範囲 (`std::numeric_limits<int>::min()` から `std::numeric_limits<int>::max()` まで、境界値含む)。Tier 1 の権威ある enforcement は firmware ハンドラ側にあります — schema range を狭めると `McpServer::Property` が十分に極端な out-of-range リクエスト (例: `pitch=200` や `pitch=INT_MIN`) をハンドラ呼び出し前に reject してしまい、ドキュメントに書かれた Tier 1 挙動が到達不能になります（詳しくは #98 を参照）。`0°` 未満のリクエストは `0°` に引き上げられ (`ESP_LOGW`)、`88°` 超のリクエストは `88°` に引き下げられ (`ESP_LOGW`)、`[0, 88]` 内かつ `[5, 85]` 外のリクエストはそのまま受け入れた上で `ESP_LOGI` のソフトシグナルを出力します。過去に `-30..+30°` を target にしていた caller はそのまま動作します（負側は `0°` にクランプされます）。

対照的に、**gateway 側の `move_head` MCP ツール** — 上記のツール一覧で LLM クライアントが見るのはこちら — は `pitch=5..85` / `yaw=-90..90` の restrictive な schema を宣言し、gateway `call_tool` ハンドラでも同じ境界を二重に enforce しています（belt-and-suspenders）。これにより、推奨範囲外のリクエストを MCP 境界で reject して、エージェントが `move_head(yaw=0, pitch=0)` のような姿勢リセット呼び出しで [#100](https://github.com/kisaragi-mochi/stackchan-mcp/issues/100) で track されているバスハング状態を誤って誘発しないようにしています。診断やリカバリ等で firmware Tier 1 ハードクランプそのものを使いたい上級用途では、`move_head` を経由せず firmware-side の `set_head_angles` デバイスツールを直接呼んでください — [#109](https://github.com/kisaragi-mochi/stackchan-mcp/issues/109) 参照。

X 軸 (yaw、`-90..+90°`) には同等のハードウェア制限はなく — M5Stack 公式が「X 軸には角度制限は不要」と明記しています — 宣言範囲全体を使えます。

下限の経緯は [#80](https://github.com/kisaragi-mochi/stackchan-mcp/issues/80)、2 層ガードへの拡張 (firmware ハードクランプ `30°` → `88°`、M5Stack 推奨 `5..85°` をソフトシグナル層に格上げ) は [#98](https://github.com/kisaragi-mochi/stackchan-mcp/issues/98) を参照してください。

## ライセンス

正規の firmware ビルドパス (`firmware/scripts/release.py stackchan`) は **end-to-end で MIT ライセンス** のバイナリを生成します。GPL-3.0 の SCServo_lib ソースは [#79](https://github.com/kisaragi-mochi/stackchan-mcp/issues/79) の移行期間中、opt-in fallback としてリポジトリに残しています。

| 範囲 | ライセンス |
|---|---|
| `gateway/`、トップレベル、**正規ビルド** の `firmware/` 全体 (`release.py stackchan` が `CONFIG_STACKCHAN_SERVO_FEETECH=y` を append し、MIT の [`feetech_scs_esp_idf`](https://github.com/necobit/feetech_scs_esp_idf) ドライバ (`firmware/components/feetech_scs/` 配下に vendor 取り込み) をリンク) | **MIT License** (`LICENSE` 参照) |
| `firmware/main/boards/stackchan/` 内の **SCServo_lib 由来ファイル** (SCS.{cc,h}, SCSCL.{cc,h}, SCSerial.{cc,h}, INST.h, SCServo.h) — `CONFIG_STACKCHAN_SERVO_SCSCL=y` (例: `sdkconfig.defaults.local` 経由) を選択した場合のみリンクされます | **GNU GPL-3.0** (`firmware/main/boards/stackchan/SCServo_lib_LICENSE.txt` 参照) |

`gateway/` は独立した Python プロセスで、ESP32 とはネットワーク経由 (WebSocket) でしか通信しないため、firmware 側のドライバ選択に関わらず **MIT License** のまま利用・派生できます。

> **既存の `firmware/sdkconfig` を持っている `idf.py` 直叩きユーザーへの注意:**
> ESP-IDF は Kconfig の選択を `firmware/sdkconfig` に永続化します。
> Kconfig の `default` 変更はそのファイルを遡及的に書き換えません。
> 正規ビルドパスは `release.py` レイヤーで (`sdkconfig_append` 経由で)
> MIT ドライバを強制するため、`release.py stackchan` 経由の再ビルドは
> 確実に MIT デフォルトのバイナリを生成します。
> `release.py` を経由せず `idf.py` を直接叩く場合で、過去に SCSCL を
> 選択した workspace では、新しい default を反映するために
> `idf.py menuconfig` の実行 (または `firmware/sdkconfig` の削除) が
> 必要になることがあります。

> **GPL-3.0 fallback ビルド (opt-in):**
> 元の SCServo_lib ソースは、#79 の移行が観察期間中である間の安全網
> として、引き続きリポジトリに同梱されています。
> ビルドに使うには `firmware/sdkconfig.defaults.local` に
> `CONFIG_STACKCHAN_SERVO_SCSCL=y` を追加してください。
> `release.py` は `sdkconfig_append` の **後に** これをマージするため、
> FEETECH デフォルトを上書きできます。その構成では firmware バイナリが
> GPL-3.0 ソースを静的リンクするため、**実質 GPL-3.0** として配布される
> ことになります。観察期間が回帰なしで終了したら、GPL ファイルは
> 削除予定です (#79 の Phase B)。

### upstream

`firmware/` は [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT) のフォーク ([kisaragi-mochi/xiaozhi-esp32](https://github.com/kisaragi-mochi/xiaozhi-esp32)) を git subtree で取り込んでいます。upstream 同期手順は [`docs/firmware-sync.md`](docs/firmware-sync.md) を参照してください。`firmware/main/boards/stackchan/` 配下の SCServo_lib ソース (`SCS.{cc,h}`, `SCSCL.{cc,h}`, `SCSerial.{cc,h}`, `INST.h`, `SCServo.h`) は [Feetech](https://www.feetechrc.com/) の SCServo SDK 由来で、同じ `kisaragi-mochi/xiaozhi-esp32` フォークの `main/boards/stackchan/` ディレクトリ経由で firmware subtree merge 時に本リポジトリに取り込まれました。これらは GPL-3.0 のままです (`firmware/main/boards/stackchan/SCServo_lib_LICENSE.txt` 参照)。

## 関連プロジェクト

- [M5Stack 公式 StackChan ドキュメント](https://docs.m5stack.com/ja/StackChan) — 想定ハードウェアの公式ドキュメント (出荷時ファーム / 配線図 / API リファレンス等)
- [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — ベースとなる ESP32 LLM クライアントファームウェア
- [stack-chan](https://github.com/stack-chan/stack-chan) — オリジナルの StackChan プロジェクト（ししかわ／石川真也 さん）
- [stackchan-arduino](https://github.com/stack-chan/stackchan-arduino) — Arduino 系の servo 制御 library（タカオ さん／mongonta0716）。本ファームウェアの SCS0009 positioning timing はこちらを参考にしています
- [m5stack-avatar](https://github.com/stack-chan/m5stack-avatar) — StackChan 系 firmware で広く使われている avatar 描画 library
- [Model Context Protocol](https://modelcontextprotocol.io) — MCP プロトコル仕様

## コントリビューション

Issue / PR 歓迎です。StackChan コミュニティで使える形を目指しています。

開発手順は [`CONTRIBUTING.md`](CONTRIBUTING.md) を参照してください。

## 商標

「StackChan」および「スタックチャン」は、stack-chan プロジェクトを発足したししかわ（石川真也）さんの登録商標です。本リポジトリでは、対象ハードウェアである M5Stack 公式 StackChan キットを指す用語としてこれらの名称を使用しています。
