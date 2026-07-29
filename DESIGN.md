# hayate-mcp 設計ドキュメント

> MCP(Model Context Protocol)サーバーを hayate アプリにマウントする
> Streamable HTTP transport。@hono/mcp が TypeScript で取った構図
> (公式 SDK へ Request/Response をブリッジする薄い層)の Python 版を、
> hayate の Request/Response + SSE の上に実装する内部設計メモ(日本語)。
> 各節は「決定 / 理由 / 却下した代替案」の形を基本とする。

## TL;DR

- **コンセプトは一文で「MCP サーバーを `register(app)` でマウント可能にする」**。
  ASGI は公式 python-sdk の API をそのまま使い、Workers は
  `WorkerMcpServer` が広告する tools capability だけを厳密に実装する。
- ASGI は SDK の低レベル `Server` にメッセージストリームで接続する。
  Workers は Pyodide の Pydantic wheel 制約を避けるため、JSON-RPC /
  discovery / tools を独立実装し、未実装の任意 capability は広告しない。
- **公開契約は dual-era**。MCP 2026-07-28 は handshake-free・POST-only・
  per-request metadata、2025-11-25 は initialize/session lifecycle。1つの endpoint
  が両方をbodyとheaderから決定的に振り分ける。
- 共通の protocol layer が標準header、`x-mcp-header`、error/HTTP mapping、
  client capability gateを担当し、ASGI/Workers間の仕様driftを防ぐ。
- 依存は CPython で `hayate + mcp`、Emscripten で
  `hayate + jsonschema + rpds-py`。エコシステムパッケージは
  「最小依存 + 理由の明記」(roadmap house style §2-4)。
- **差別化は Workers**: SSE / Durable Object は本体 research §5 で workerd 実機検証済み。
  Cloudflare 公式の remote MCP / Agents SDK は TS のみで、Python MCP on Workers は空白地帯。
- 中期の本命 story は hayate-auth との合流(MCP OAuth):
  「**MCP サーバーとその認可サーバーを 1 つのアプリにマウントできる唯一の Python スタック**」。

公開例は SDK v2 の低レベル handler constructor を使う。完全な例は
[README.md](README.md) と [examples/echo/server.py](examples/echo/server.py)。

---

## 1. なぜ作るか

### 1.1 現状の摩擦(2026-07-22 調査)

- Python は MCP サーバーの最大勢力(公式 SDK / FastMCP / 無数の実装)なのに、
  「**自分の Web アプリと同居させる**」体験が弱い。FastMCP v3 が事実上標準だが、
  既存アプリへの統合は Starlette mount 経由で、nested lifespan の罠や
  公式 SDK 側の mount 不具合(python-sdk #1367)が残る。
- Cloudflare の remote MCP / Agents SDK は TS のみ。Python で edge MCP を出す経路がない。
- Hono は @hono/mcp(公式 TS SDK への Request/Response ブリッジ)でこの席を確保済み。
  Python 側の同型の席が空いている。

### 1.2 hayate の勝ち筋

Streamable HTTP transport の構成要素は POST/GET + SSE + セッション —
**すべて hayate が標準語彙で持っている**。ASGI の scope/receive/send を介さず
Request/Response 境界で切れるため、@hono/mcp と同じ薄さの transport が書けるのは
Python では hayate だけ。さらに workerd 実機検証済みの SSE / DO がそのまま
「remote MCP on Python Workers」に転用できる。

### 1.3 勝負しない領域

ツール定義 DSL(SDK / FastMCP の領分)、MCP クライアント、サーバーレジストリ / カタログ、
stdio transport(SDK が既に提供)。

---

## 2. 規範とする標準(Normative References)

| 対象 | 文書 | 対応 |
|---|---|---|
| MCP | modelcontextprotocol.io specification **2026-07-28** と互換契約 **2025-11-25** | ASGI は SDK 2.x、Workers は広告した tools surface。era共通transport検証は `protocol.py` |
| MCP 2026 lifecycle | handshake-free stateless lifecycle / `server/discover` | 全POSTにprotocolVersion/clientCapabilities metadata。session idなし。旧methodは `-32601` + HTTP 404 |
| MCP-Protocol-Version / routing headers | 2026 transport + SEP-2243 | body metadataと `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` / `Mcp-Param-*` を照合。不一致 `-32020` + HTTP 400 |
| MCP 2025 lifecycle | initialize/session + Streamable HTTP | ASGI はinitialize responseの合意版をsessionごとに固定。Workersはstateless compatibility |
| MRTR / requestState | SEP-2322 | ASGI SDKの `InputRequiredResult` と暗号化・期限・principal/audience bindingを利用 |
| Caching | SEP-2549 | cacheable resultに `ttlMs` / `cacheScope`。ASGIはSDK、Workersはserver設定 |
| JSON-RPC 2.0 | jsonrpc.org | ASGI は SDK、Workers は単一 message envelope・標準 error code・notification 202 を検証 |
| SSE | WHATWG HTML | ストリーミング応答。本体 `sse.py` を利用 |
| Origin 検証 | MCP spec(**MUST**)+ RFC 6454 | present かつ invalid な Origin は 403(§5) |
| 認可 | MCP Authorization(OAuth 2.1 / RFC 9728 Protected Resource Metadata) | v0.4(§5、実装済み) |

---

## 3. アーキテクチャ

### 3.1 層構造

```
ユーザーコード:  SDK Server(ASGI) | WorkerMcpServer(Workers)
────────────────────────────────────────────────────────────
protocol.py: era判定 / header codec・照合 / capability gate / error mapping
────────────────────────────────────────────────────────────
2026: server/discover + stateless POST + resultType/cache/MRTR
2025: initialize + session-compatible Streamable HTTP
────────────────────────────────────────────────────────────
dispatch:  公式 SDK 2.x       | Workers-native tools runtime
────────────────────────────────────────────────────────────
hayate-mcp:  McpMount         | WorkerMcpMount
   POST   → JSON-RPC メッセージ受信 → 単発 JSON 応答 or SSE ストリーム
   GET    → 2025 ASGI: サーバー起点 SSE / 2026・Workers: 405
   DELETE → 2025 ASGI: セッション終了 / stateless: no-op
   ASGI 2025だけMcp-Session-Id / 共通: Origin・auth検証
─────────────────────────────────────────────
hayate:  register(app) → app.on("GET"/"POST"/"DELETE", path)
─────────────────────────────────────────────
SessionStore: memory(ASGI stateful) | stateless(Workers)
```

### 3.2 SDK への接続方式(決定)

SDK の低レベル `Server` は anyio のメモリストリーム対で transport 非依存に動作する。
hayate-mcp は HTTP 側でメッセージを受け、ストリーム経由で `Server` に渡し、
応答を JSON / SSE に書き戻す。

- **理由**: プロトコル追従コストを SDK に外部化する。spec は改訂が速く、
  transport だけに表面積を絞るのが個人メンテナ体制で唯一持続可能な形。
  @hono/mcp が TS で同じ判断をして成立している。
- **却下**: フル自前実装(ゼロ依存化)— JSON-RPC + capabilities + ツール登録 API の
  再発明で YAGNI。spec 追従の保守が二重になる。
- **却下**: FastMCP v3 への直接依存 — ASGI 前提が深く Request/Response 境界で切れない。
  ただし FastMCP で定義したツール群が SDK `Server` に変換できるなら
  入力として受け入れる(要検証)。

### 3.3 Workers の独立 runtime(決定、v0.8)

Workers では `WorkerMcpServer` / `WorkerMcpMount` を使う。2026-07-28 の
`server/discover`、stateless `tools/list` / `tools/call`、標準/custom header、
result envelope、cache hint、server identity、JSON Schema 2020-12を実装する。
2025-11-25のinitialize/ping/toolsも互換pathとして保持する。resources / prompts /
logging / sampling / Tasks / GET SSE は広告しない。

- **理由**: 公式 SDK 1.28.1 は Pydantic 2.11+ を要求する一方、pywrangler の
  Pyodide index は Pydantic 2.10.6 / pydantic-core 2.27.2 まで。旧 SDK を残すと
  Workers だけ 2025-06-18 になり、全ランタイム最新準拠という契約を破る。
- **安全境界**: schema validator は最初のリクエスト内で遅延 import し、
  workerd の global-scope entropy 制約を回避する。未知の例外はログにだけ残して
  model には sanitize した `isError` result を返す。入力 schema 違反も公式 SDK と
  同じく model-correctable な `isError` とし、未知 tool / malformed envelope だけを
  protocol error にする。
- **host context**: `register(app)` 経由では現在の hayate `Context` を ContextVar で
  tool handler へ伝播し、`get_request_context()` から header / `c.env` / `c.get()` を
  再利用できる。model-visible tool argument に認証・binding を混ぜず、並行 request 間で
  state を共有しない。純粋な `fetch(Request)` には host context を捏造しない。
  app 固有の認証・rate limit・依存障害は `WorkerProtocolError` で JSON-RPC code と
  HTTP status/header を同時に保持する。ただし2026 coreがHTTP statusを規定する
  reserved error codeではprotocol mappingを優先する。
- **capability honesty**: toolがsampling等を必要とする場合は
  `required_capabilities` を宣言し、未宣言clientをhandler前に `-32021` で拒否する。
  Tasksはcoreからextensionへ移動したため、extension negotiationと実装が揃うまで
  `capabilities.extensions` に広告しない。
- **適合証拠**: unit test、公式 SDK model validation、uvicorn E2E、
  workerd 上の公式 SDK client E2E を CI に常設する。

---

## 4. セッション管理

- ASGI stateful mode は `Mcp-Session-Id`(spec 準拠)を発行し、
  `SessionStore` protocol で保持。
  house style 通り protocol 注入(hayate-auth の Adapter と同型)。
- **このsession pathは2025 request専用**。2026 requestは同じmount設定でも常に
  self-contained/statelessで、`Mcp-Session-Id` を発行も受理もしない。
- 既定は in-memory(単一プロセス)。Workers は両eraとも stateless で
  session idを発行しない。将来サーバー起点ストリームやセッション状態を
  Workers に追加する場合は Durable Object が必要(§11.2)。
- resumability(`Last-Event-ID`)は spec 上 optional。**判断(2026-07-23): v0.2 では非対応** — 再生バッファはセッションが isolate をまたいで生きる DO ストア側に置くのが正しい構造で、メモリストアに足しても本番で意味を成さないため(auth 本番実測でも isolate 揮発を確認)。DO 版と同時に再判断。

## 5. セキュリティ

- **Origin 検証は既定 ON**(spec MUST。`trusted_origins` を指定させる)。
  localhost バインド時の注意も README に明記。
- v0.1 は authless(spec 上 optional)。
- **v0.4: OAuth 2.0 Resource Server 側を実装(出荷済み)**。`McpMount(authorization=Authorization(...))`
  で MCP Authorization(2026-07-28 / 2025-11-25)+ RFC 9728 に対応:
  - `/.well-known/oauth-protected-resource` に Protected Resource Metadata(RFC 9728)を提供
    (`resource` / `authorization_servers` / `bearer_methods_supported` / `scopes_supported`)。
    このエンドポイントはトークン不要の公開ディスカバリ。
  - 未認証リクエストは **401 + `WWW-Authenticate: Bearer resource_metadata="…"`**(RFC 9728 §5.1)
    でメタデータ URL を案内。Claude 等のクライアントはここから AS を発見する。
  - トークン検証は `verify_token(token) -> claims | None` の注入(RFC 6750 Bearer)。
    verifierはsignature/issuer/expiryに加えRFC 8707のresource/audience一致まで検証し、
    claimsを返すことが契約。
    **AS(トークン発行)側は分離**し、hayate-auth の将来の AS モード、または任意の
    RFC 6749 AS を指せる。これが「MCP サーバー + その AS を 1 アプリに」story の RS 半分。
- ~~**残**: hayate-auth の AS モード(/authorize・/token・動的クライアント登録)は auth 側の
  別機能。揃えば同一アプリに MCP + AS をマウントする完全形になる(証拠駆動)。~~
  **達成(2026-07-23、auth 0.6.0)**: auth の AS モード(DESIGN §19)が出荷され、
  `verify_token=auth.oauth_token_verifier(resource=...)` で完全形が成立。
  公式 SDK クライアントの OAuth フル一周が auth 側 examples/mcp-oauth の CI に常設
  (実測ログ: auth `docs/research/authorization-server.md`)。

## 6. 実行モデル / Workers 制約

### 6.1 ASGI

`McpMount` は公式 SDK `Server` を anyio stream で駆動する。stateful では
`Mcp-Session-Id` と GET SSE を2025 clientへ提供する。2026 requestと
`stateless=True` の2025 compatibility pathは1リクエスト内で `Server.run(...)`
を完結させる。公式 SDK は `>=2.0.0,<3`。progress等の中間notificationがある
2026 responseは、final responseまでを1本のbounded POST SSEで返す。
`subscriptions/listen` handlerを登録したserverはresponse-ownedなlive SSEへ
切り替え、ack-first、subscription ID、filter、15秒keepalive、body close時の
task cancellationをtransportが担う。

### 6.2 Workers

`WorkerMcpMount` は常に stateless。2026はdiscover/tools、2025は
initialize/ping/toolsを直接dispatchする。SDK / Pydantic / anyio task groupは
bundleに入らず、detached taskも作らない。GETは405、DELETEはno-op 200。

依存 marker は次の通り:

- CPython: `mcp>=2.0.0,<3`
- Emscripten: `jsonschema>=4.20,<5` と
  pywrangler 1.15 の Pyodide 0.28.3 index に存在する
  `rpds-py==0.23.1`

これにより両ランタイムが2026-07-28をnativeに処理し、2025-11-25 clientも
維持する。Workersはoptional capabilityを部分実装せず、未広告にすることで
仕様上の表面積をtoolsに限定する。

### 6.3 リビジョン追従

公開契約は「current + 直前のhandshake era」の二層とする。新eraを追加するときは
共有`protocol.py`、ASGI SDK、Workers-native wire、conformance fixtureを同じ変更で
更新する。任意extensionはcore revisionと分離し、capability advertisement、
client opt-in、handler、negative testがすべて揃ったときだけ有効化する。

## 7. テスト戦略

- transport 単体は `await mount.fetch(Request(...))` 直叩き(house style の純関数コア)。
- official conformance は2025-11-25の30 scenarioと2026-07-28 coreの40 scenarioを
  expected-failureなしで固定する。MRTR/requestState改ざん、cache、header、
  HTTP status/error codeもdenominatorに含める。
- E2E は **MCP Inspector と Claude Code を実クライアント**として受け入れ基準に組み込む
  (モックだけで通すと workers-py ラッパー形状事件の再演になる — 本体 CLAUDE.md の教訓)。
- 3 ランタイム: pytest 直 / uvicorn / workerd。

## 8. スコープ外(YAGNI リスト)

| やらないこと | 理由 |
|---|---|
| MCP クライアント | 需要の証拠待ち。hayate-fetch と合流の可能性があるため単独では作らない |
| stdio transport | SDK が提供済み。Web アプリへのマウントという本パッケージの存在意義の外 |
| ツール定義 DSL / スキーマ生成 | SDK / FastMCP の領分 |
| サーバーレジストリ / カタログ / ホスティング | 別事業 |
| WebSocket transport | spec 外(Streamable HTTP が現行標準) |

## 9. リスクと対応

| リスク | 対応 |
|---|---|
| SDK の最新版が Pyodide に解決できない | Workers-native runtime で SDK / Pydantic を bundle から除去。workerd + 公式 SDK client の境界 E2E を CI 固定 |
| MCP spec の改訂速度 | era判定とtransport invariantを共有層へ集約。ASGI/Workers/fixtureを同一PRで更新しdual-era conformanceを必須化 |
| FastMCP v3 が同領域を埋める | 土俵を変える: Workers + hayate-auth 合流(§5)。汎用 ASGI 統合では競わない |
| PyPI 名スクワット | `hayate-mcp` 空き確認 2026-07-22。0.0.x 早期公開で確保 |

## 10. マイルストーン

| 版 | 内容 | 受け入れ基準 |
|---|---|---|
| ~~**spike**~~ | **完了(2026-07-22)**: SDK import + echo ツールの in-process 一周を workerd で確認 | ✅ historical result。SDK 1.28 の wheel 制約により v0.8 で Workers-native runtime へ更新 |
| ~~**v0.1**~~ | **完了(2026-07-22)**: McpMount(POST=JSON 単発 / DELETE / GET=405)+ Mcp-Session-Id + memory SessionStore(idle eviction)+ Origin 検証 | ✅ **MCP Inspector CLI から接続し tools/list・tools/call 実行を実測**(uvicorn)。✅ 公式 SDK クライアント(`streamable_http_client` + `ClientSession`)での実 HTTP 一周を E2E テストとして CI に常設。テスト 16。✅ **Claude Code 実機接続も実測(2026-07-23)**: `claude mcp add --transport http` → `claude mcp list` で Connected、ヘッドレス実行で echo ツールの呼び出しに成功。受け入れ基準は両実クライアントで完全達成 |
| v0.2 | **出荷(2026-07-23)**: GET SSE ストリーム(1 本/セッション、409 で多重拒否、close で終端。テスト 20)+ resumability 判断(§4) | GET SSE ✅ |
| v0.3 | **出荷(2026-07-23)**: `stateless=True` モード(§6.1)。**Cloudflare Workers で緑化**(DO 不要) | ✅ **workerd 上で MCP フル一周(initialize → tools/list → tools/call)を curl と MCP Inspector CLI で実測**。テスト 27(stateless 7 追加) |
| v0.4 | **出荷(2026-07-23)**: OAuth 2.0 Resource Server 側(RFC 9728 Protected Resource Metadata + Bearer 検証 + 401/`WWW-Authenticate`)。§5 | ✅ 認可済みクライアントのみ接続可・authless 構成も選択可。テスト 35(authorization 8 追加)。AS 側(トークン発行)は hayate-auth の将来機能 |
| v0.5 | **出荷(2026-07-23)**: 最新 stable **2025-11-25** 準拠(SDK 1.28.1)+ `MCP-Protocol-Version` ヘッダ検証(§2) | ✅ 当時は CPython が 2025-11-25、旧 SDK の Workers は 2025-06-18。v0.8 で Workers-native 2025-11-25 に置換。両ランタイムで無効版 400 を実測。テスト 42(protocol-version 7 追加) |
| v0.6 | **出荷(2026-07-23)**: PRM の well-known URI を **RFC 9728 §3.1 の path-insertion 形式に是正**(`https://h/mcp` → `https://h/.well-known/oauth-protected-resource/mcp`)。0.5.x までは well-known をパスの後ろに連結した URL を広告しつつルート形式を serve しており、広告 URL が 404 を指していた(auth の AS spike on workerd が発見。クライアントはフォールバック探索で動いていた) | ✅ 広告 URL と serve パスの一致をパス付き / パス無し resource の両方でテスト固定。旧 2 形式は 404。テスト 44 |
| v0.7 | **出荷準備(2026-07-24)**: MCP 2025-11-25 transport media 契約、Principal/context、global/tool scope、session owner binding、secure URI、LazyMcpMount、`py.typed`、CPython SDK `>=1.28.1,<2` | 公式 SDK E2E を含む 60 テスト。strict mypy 6 files。FolioMCP 相当の AS→resource→tool principal 横断試験 ✅ |
| v0.8 | **出荷準備(2026-07-24)**: Workers-native lifecycle/ping/tools runtime。旧 SDK 依存を除去し全 runtime 2025-11-25 | JSON Schema 2020-12、OAuth/scope、workerd、公式 SDK client の相互運用を CI 固定。SDK/Pydantic が Workers bundle に無いことも検証 |
| v0.12 | **実装完了(2026-07-30)**: MCP 2026-07-28 + 2025-11-25 dual-era、SDK 2.x、stateless discovery、標準/custom header、cache hints、resultType、capability gate、MRTR、暗号化requestState、bounded POST SSE、live subscriptions | official conformance 70/70(旧30+新40、subscription実経路を含む)、pytest、mypy、workerd、SDK client E2E |
| v1.0 | API 凍結 | 本体 v1.0 より後 |

## 11. Workers 対応(2026-07-23、緑化)

### 11.1 Workers-native stateless runtime

**`WorkerMcpServer` + `WorkerMcpMount` + 素の `to_workers(app)` で対応**。DO は不要。

- 2026のserver/discover/toolsと2025のinitialize/ping/toolsを1リクエスト内で完結し、
  detached taskを作らない。
- `tools` だけをcapabilityとして広告し、Tasks extensionは未広告。
  task augmentationが送信された場合は `-32601`。
- JSON Schema validator は request scope で初期化し、global import entropy 制約を回避。
- サーバー起点メッセージとセッション跨ぎ状態は持たない。GET は 405、DELETE は no-op 200。

### 11.2 DO によるステートフル Workers(将来)

サーバー起点ストリームやセッション状態を Workers で持つには DO が要るが、
`McpSession` の `asyncio.ensure_future(server.run(...))`(リクエストを跨ぐ detached task)は
DO 実行モデルに反して isolate を hard-crash させる(2026-07-23 に確定、`docs/research/workers-do.md`。
POST-body DO forward とバンドル汚染は原因から除外済み)。解くには DO 内で
`ctx.waitUntil` / hibernation で Server task を明示的に生かす設計が要る。**証拠駆動で保留**
(stateless で大半のツールサーバーは足りるため)。DO 用スキャフォールド `hayate_mcp.workers` は
未達のため v0.3 で**削除**した。

### 決定済み(2026-07-22)

| 項目 | 決定 |
|---|---|
| 名前 | **hayate-mcp**(配布名)/ `hayate_mcp`(import 名) |
| リポジトリ | `hayatepy/hayate-mcp`。private 開始、v0.1 完成時に公開判断 |
| ライセンス / 最低 Python | MIT / 3.12(本体に合わせる) |
| 依存 | CPython: `hayate` + `mcp`。Workers: `hayate` + `jsonschema` + Pyodide `rpds` wheel |
