# Workers Durable Object session store — verification log

> **解決(2026-07-23)**: Workers 対応は **stateless モード**(`McpMount(stateless=True)` +
> 素の `to_workers`、DO 不要)で緑化した(DESIGN §11.1、workerd 上で MCP フル一周を
> Inspector CLI 実測)。本ログは「DO でステートフルを持つ将来案」がなぜ現状ブロックされるかの
> 記録として残す。DO スキャフォールド `hayate_mcp.workers` は未達のため削除済み。
> 本体 research §5 と同じ規律: 実機で緑になっていないものはチェックしない。

## 現状(2026-07-23)

`examples/workers/`(outer Hayate app が `/mcp` をセッション別 Durable Object に
ルーティングし、DO 内で `McpMount` がインメモリで走る McpAgent 型構成)を
`pywrangler dev`(ローカル workerd)で検証中。**worker と DO クラスの登録までは成功、
DO へのサブリクエスト dispatch が workerd レベルの `internal error` で失敗する**。

### 解決済みの障害(道中の学び)

1. **`mcp` の global-scope import 不可**: SDK の依存 `rpds` が import 時に
   `getRandomValues` でエントロピーを引く。workerd は Worker の **global scope 評価**でも
   **Durable Object のコンストラクタ**でもこれを禁止する。
   → `hayate_mcp/__init__.py` を PEP 562 の遅延 `__getattr__` にし、DO の `handle` 内で
   初回リクエスト時(許可されたスコープ)に mount を組む形にして回避。
2. **DO クラス名**: `to_durable_object` は factory の `__name__` で DO クラスを登録する
   (本体 CLAUDE.md の罠)。`mcp_durable_object` の内部 factory は `factory` という名前だった
   → `class_name` 引数で `__name__` を wrangler.toml の `class_name` に合わせて解決。

### 切り分け結果(2026-07-23 追加) — POST-body DO forward は「シロ」

本体側で最小 repro(`@to_durable_object` の counter に POST ルート + outer から
`forward(c, stub)` で POST 転送)を作成 → **POST ボディは DO に正しく届く**。
`getByName` / `get(newUniqueId())` / `get(idFromString())` の 3 経路すべてで POST 成立
(本体 research §5 に追記済み)。つまり **DO forward の一般機構と id API は問題なし**。
route_to_session は素の `forward` に戻した(rebuild 不要と確定)。

### ブロッカー確定(2026-07-23) — anyio `Server` の DO 内ライフサイクル

同じ `forward` パターンでも **hayate-mcp の DO だけ** `internal error`(DO 側トレース無し)。
counter repro との差分 2 候補のうち、

- **候補 2(vendor バンドル汚染)は除外**: `.venv-workers` の pywin32 / win32com など
  Windows ホスト専用パッケージを削り python_modules と揃えた**クリーンなバンドルでも
  同じ `internal error`**。バンドル汚染は原因ではない。
- **候補 1(anyio `Server` の DO 内実行)を確定**: `McpSession.__init__` が
  `asyncio.ensure_future(server.run(...))` で**リクエストを跨いで生きる detached
  バックグラウンドタスク**を起こす。Cloudflare の DO / Workers 実行モデルは、非同期処理を
  リクエストコンテキスト内で await(または `ctx.waitUntil`)することを要求するため、
  この寿命の長い task が isolate を hard-crash させ、Python 例外トレースの出ない
  workerd レベルの `internal error` になる。**POST body でも id API でもバンドルでもなく、
  「MCP SDK の永続接続前提の Server を bounded な DO リクエストで回す」という構造的ミスマッチ**。

### 次アクション(v0.3、要設計変更)

DO 内では detached タスクを使わず、**リクエストごとに Server を initialize → 1 メッセージ処理
→ teardown する同期的経路**(バックグラウンド task 無し)にするか、`ctx.waitUntil` /
DO hibernation で Server task を明示的に生かす。どちらも v0.1 の in-process transport とは
別コードパスになるため、mount 側に「DO モード = per-request server」の分岐を設計してから着手する。
その上で MCP Inspector / SDK クライアントから wss/https 接続を実測(受け入れ基準)。

**判断**: v0.2 は「GET SSE ストリーム(CPython 検証済み・テスト常設)」を確定分として出荷済み。
Workers + DO は **POST forward はシロ・バンドル汚染も除外・ブロッカーは anyio Server の DO 内
ライフサイクルに確定**した状態で v0.3(要設計変更)に継続。実機で緑になっていないものはチェックしない。
