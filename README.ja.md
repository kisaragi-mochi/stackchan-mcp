[English](README.md) | **日本語**

# stackchan-mcp

**M5Stack 公式 [StackChan](https://docs.m5stack.com/ja/StackChan)** (2025年 Kickstarter 出荷キット) を任意の LLM クライアントから操作するための MCP (Model Context Protocol) ブリッジ。

> オリジナルの [stack-chan プロジェクト (タカヲさん)](https://github.com/mongonta0716/stack-chan) のコミュニティから生まれ、M5Stack 公式が製品化した StackChan キットを対象としています。

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

## 想定ハードウェア

**M5Stack 公式 [StackChan キット](https://docs.m5stack.com/ja/StackChan)** (Kickstarter 2025 出荷版)。公式ドキュメントの[出荷時ファームウェア](https://docs.m5stack.com/ja/StackChan#%E5%87%BA%E8%8D%B7%E6%99%82%E3%83%95%E3%82%A1%E3%83%BC%E3%83%A0%E3%82%A6%E3%82%A7%E3%82%A2)を本リポジトリの firmware で置き換える形で動作します。

| 部品 | 仕様 |
|---|---|
| **本体** | M5Stack CoreS3 (ESP32-S3, 16MB Flash, 8MB PSRAM) |
| **首サーボ** | SCS0009 ×2 (yaw + pitch、シリアルバス、TX=GPIO6, RX=GPIO7) |
| **カメラ** | GC0308 (DVP, 320×240) |
| **タッチ** | FT6336 / Si12T |
| **ディスプレイ** | ILI9342 (SPI, 320×240) |

> 自作の stack-chan (タカヲさん版オリジナル設計) でも、上記のピンアサイン・I2C アドレスが一致していれば動く可能性があります。動作報告・修正 PR 歓迎です。

## ツール一覧 (gateway 経由で MCP クライアントが呼べる)

| ツール | 説明 | 状態 |
|---|---|---|
| `get_status` | ゲートウェイ接続状態 | ✅ |
| `get_device_info` | ESP32 デバイス状態 (バッテリー/音量/WiFi 等) | ✅ |
| `take_photo(question?)` | カメラ撮影 → JPEG 保存 → パス返す | ✅ |
| `set_volume(volume)` | スピーカー音量 (0-100) | ✅ |
| `set_brightness(brightness)` | 画面明るさ (0-100) | ✅ |
| `move_head(yaw, pitch, speed?)` | 首を動かす (サーボ) | ✅ |
| `get_touch_state` | タッチセンサ状態 (press/release/stroke 等) | ✅ |
| `set_avatar(face)` | アバター表情切替 (`idle` / `happy` / `thinking` / `sad` / `surprised` / `embarrassed`)、または `off` でアバターを隠し blink も停止して下層の WiFi 設定 / OTA / 設定画面を露出。他 face を指定するとアバター + blink が復帰 | ✅ |
| `set_blink(state)` | 瞬き ON/OFF | ✅ |
| `set_mouth(state)` | 口開閉（one-shot、次の呼び出しまで保持） | ✅ |
| `set_mouth_sequence(steps)` | TTS リップシンク用に `{shape, duration_ms}` のリストをデバイス側でキュー再生（ステップごとの WebSocket RTT ゆらぎなし） | ✅ |
| `check_vm_en` | サーボ電源 (VM EN HIGH) 状態確認 | ✅ |

詳細スキーマは `gateway/README.md` 参照。

## クイックスタート

### 1. ファームウェア書き込み (CoreS3)

```bash
cd firmware
docker run --rm -v $PWD:/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
# → releases/v2.2.6_stackchan.zip

# フラッシュ (CoreS3 を USB 接続後)
esptool.py --chip esp32s3 --port /dev/cu.usbmodem1101 -b 460800 \
  write_flash 0x0 build/merged-binary.bin
```

WiFi 設定は ESP32 が起動後にスマホで設定 UI に接続して行う (xiaozhi-esp32 標準フロー)。

### WebSocket gateway URL と認証トークンの設定

ファームウェアはゲートウェイ接続のために以下の NVS キーを参照します:

- `websocket.url` — ゲートウェイ WebSocket URL (例: `ws://192.168.1.100:8765/`)
- `websocket.fallback_url` — `websocket.url` に接続できない、または server hello が完了しない場合に試す 2 番目の gateway URL
- `websocket.token` — `Authorization: Bearer <token>` で送信される bearer トークン。ゲートウェイ側の `STACKCHAN_TOKEN` / `BEARER_TOKEN` と照合される (両方空にすれば認証スキップ)

設定方法は実用的に 3 つ:

1. **Kconfig によるビルド時デフォルト (開発者推奨)**: `idf.py menuconfig` → `Component config` → `Xiaozhi Assistant` を開き、以下を設定:
   - `Default WebSocket gateway URL (fallback when NVS is empty)` →
     `CONFIG_DEFAULT_WEBSOCKET_URL` (例: `ws://192.168.1.100:8765/`)
   - `Fallback WebSocket gateway URL` →
     `CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL`
   - `Default WebSocket auth token (fallback when NVS is empty)` →
     `CONFIG_DEFAULT_WEBSOCKET_TOKEN` (ゲートウェイが認証不要なら空のままで OK)

   デフォルトでは、対応する NVS キーが空のときだけこの値が使われます。新規デバイスへの初回フラッシュではちょうど期待通りに動作します。primary と fallback の両方を設定した場合、ファームウェアは決まった順番で候補を試し、WebSocket の server hello まで完了した最初の候補を使います。

2. **デバイス上の WiFi 設定 UI を使う（新規ユーザー向け推奨）**: デバイスが WiFi 設定モードになっているとき、`http://192.168.4.1` のキャプティブポータルを開き、**Advanced** タブに切り替えて以下を入力します:
   - **WebSocket Gateway URL**（例: `ws://<gateway-host>:8765/`） — primary な gateway 候補。
   - **Fallback Gateway URL**（例: `wss://<node>.<tailnet>.ts.net/`） — 任意の 2 番目の候補。primary が server hello 完了に失敗したときだけ試行されます。
   - **Gateway Token** — 任意の bearer トークン。設定時は両候補に対して `Authorization: Bearer <token>` ヘッダで送信されます。WiFi 設定 UI の AP は未認証で開かれるため、GET エンドポイントはトークンの有無だけを返し、現在の値は表示されません。空のまま送信すると既存トークンが保持され、新しい値を入力すると更新、❌ ボタンで build-time の `CONFIG_DEFAULT_WEBSOCKET_TOKEN` に戻ります。Kconfig 既定値が未設定のビルドでは ❌ が認証なしを意味しますが、既定値が組み込まれているビルドでは ❌ で実際に認証が解除されるわけではなく、その既定値に戻る点に注意してください。組み込み既定値があるビルドで認証なしの gateway に向けたい場合は、Kconfig 既定値を空にして再ビルドするか、gateway 側の token を build-time 既定値に揃えてください。

   送信すると値が `websocket` NVS namespace（`websocket.url` / `websocket.fallback_url` / `websocket.token`）に永続化され、次回起動時に読み込まれます。pre-built ファームウェアを使うエンドユーザー向けの想定経路です。URL フィールド横の ❌ ボタンでクリアしてから再度送信すると、対応する `CONFIG_DEFAULT_WEBSOCKET_*` Kconfig 値（Kconfig 既定値が未設定なら「fallback なし」）に戻ります。

3. **NVS に直接 `websocket.url` / `websocket.fallback_url` / `websocket.token` を書き込む（上級者向け）**: 例えば独自の NVS 書き込みツールをシリアル経由で使うケース。WiFi 設定 UI と同じ永続化セマンティクス。バッチ provisioning などで主に使います。

4. **一時的なソース hardcode (非推奨)**: `websocket_protocol.cc` を編集すればローカル実験はアンブロックできますが、commit には残さないようにしてください。

よく使う gateway URL 構成:

| モード | Primary URL | Fallback URL |
| --- | --- | --- |
| ローカルのみ | `ws://<gateway-host>:8765/` | 空 |
| Tailscale のみ | `wss://<node>.<tailnet>.ts.net/` | 空 |
| ローカル優先 + リモート fallback | `ws://<gateway-host>:8765/` | `wss://<node>.<tailnet>.ts.net/` |

#### 既存デバイス (古い NVS) — `CONFIG_FORCE_DEFAULT_WEBSOCKET_URL`

以前に上流の xiaozhi-esp32 ファームウェアが書き込まれたことがあるデバイスにフラッシュする場合、NVS には上流の OTA-config パスが書き込んだ `websocket.url=wss://api.tenclass.net/...` が既に存在します。この場合、上記オプション 1 の empty-NVS fallback は **発動せず**、デバイスはローカルゲートウェイではなく tenclass を呼び続けます。現時点で `websocket` NVS namespace を選択的にクリアするランタイムツールはありません。

NVS を全消去 (WiFi 認証も飛ぶ) せずにこれを回避するには、force-override スイッチを有効化します:

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
CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL="wss://<node>.<tailnet>.ts.net/"
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

## ライセンス

このリポジトリはデュアルライセンス構成です。

| 範囲 | ライセンス |
|---|---|
| 全体 (`gateway/`, トップレベル, `firmware/` の大部分) | **MIT License** (`LICENSE` 参照) |
| `firmware/main/boards/stackchan/` 内の **SCServo_lib 由来ファイル** (SCS.{cc,h}, SCSCL.{cc,h}, SCSerial.{cc,h}, INST.h, SCServo.h) | **GNU GPL-3.0** (`firmware/main/boards/stackchan/SCServo_lib_LICENSE.txt` 参照) |

これは Feetech の SCServo SDK が GPL-3.0 で配布されているための制約です。SCServo_lib を静的リンクする **firmware バイナリ全体は実質 GPL-3.0** として配布されることになります。

一方、`gateway/` は独立した Python プロセスで、ESP32 とはネットワーク経由 (WebSocket) でしか通信しないため、**MIT License** のまま利用・派生できます。

### upstream

`firmware/` は [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT) のフォーク ([kisaragi-mochi/xiaozhi-esp32](https://github.com/kisaragi-mochi/xiaozhi-esp32)) を git subtree で取り込んでいます。upstream 同期手順は [`docs/firmware-sync.md`](docs/firmware-sync.md) を参照してください。SCServo_lib は公式 [stack-chan](https://github.com/mongonta0716/stack-chan) (タカヲさん) から移植したファームウェアコンポーネントです。

## 関連プロジェクト

- [M5Stack 公式 StackChan ドキュメント](https://docs.m5stack.com/ja/StackChan) — 想定ハードウェアの公式ドキュメント (出荷時ファーム / 配線図 / API リファレンス等)
- [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — ベースとなる ESP32 LLM クライアントファームウェア
- [stack-chan](https://github.com/mongonta0716/stack-chan) — オリジナルの StackChan プロジェクト (タカヲさん)
- [Model Context Protocol](https://modelcontextprotocol.io) — MCP プロトコル仕様

## コントリビューション

Issue / PR 歓迎です。StackChan コミュニティで使える形を目指しています。

開発手順は [`CONTRIBUTING.md`](CONTRIBUTING.md) を参照してください。
