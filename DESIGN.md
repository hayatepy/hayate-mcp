# hayate-mcp 設計ドキュメント

> MCP(Model Context Protocol)サーバーを hayate アプリにマウントする
> Streamable HTTP transport。@hono/mcp が TypeScript で取った構図
> (公式 SDK へ Request/Response をブリッジする薄い層)の Python 版を、
> hayate の Request/Response + SSE の上に実装する内部設計メモ(日本語)。
> 各節は「決定 / 理由 / 却下した代替案」の形を基本とする。

## TL;DR

- **コンセプトは一文で「MCP サーバーを `register(app)` でマウント可能にする」**。
  ツール定義は公式 python-sdk(modelcontextprotocol/python-sdk)の API をそのまま使い、
  本パッケージは **transport(HTTP 境界)だけ**を実装する。
- プロトコル(JSON-RPC / capabilities / ツール実行)は再実装しない。
  SDK の低レベル `Server` にメッセージストリームで接続する(@hono/mcp と同じ判断)。
- 依存は `hayate` + `mcp`(SDK)。ゼロ依存はコア(hayate 本体)の原則であり、
  エコシステムパッケージは「最小依存 + 理由の明記」(roadmap house style §2-4)。
- **差別化は Workers**: SSE / Durable Object は本体 research §5 で workerd 実機検証済み。
  Cloudflare 公式の remote MCP / Agents SDK は TS のみで、Python MCP on Workers は空白地帯。
- 中期の本命 story は hayate-auth との合流(MCP OAuth):
  「**MCP サーバーとその認可サーバーを 1 つのアプリにマウントできる唯一の Python スタック**」。

```python
from mcp.server import Server          # 公式 SDK — ツール定義はここに書く
from hayate import Hayate
from hayate_mcp import McpMount

server = Server("my-tools")
# … @server.list_tools() / @server.call_tool() …

app = Hayate()
McpMount(server, path="/mcp").register(app)   # これだけ
```

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
| MCP | modelcontextprotocol.io spec — **Streamable HTTP transport**。最新 stable **2025-11-25**(SDK `LATEST_PROTOCOL_VERSION`)。**2026-07-28 が RC**(stateless core) | transport 実装の唯一の根拠。対応リビジョンは SDK に追従(§6.2)。`MCP-Protocol-Version` ヘッダ検証を実装 |
| MCP-Protocol-Version | Streamable HTTP transport(2025-06-18+) | initialize 後の全リクエストに必須。**未対応値は 400 MUST**。SDK の `SUPPORTED_PROTOCOL_VERSIONS` で判定。ヘッダ欠落は後方互換で通す(spec は 2025-03-26 仮定) |
| JSON-RPC 2.0 | jsonrpc.org | ワイヤ形式(SDK が処理。本パッケージは触らない) |
| SSE | WHATWG HTML | ストリーミング応答。本体 `sse.py` を利用 |
| Origin 検証 | MCP spec(**MUST**)+ RFC 6454 | present かつ invalid な Origin は 403(§5) |
| 認可 | MCP Authorization(OAuth 2.1 / RFC 9728 Protected Resource Metadata) | v0.4(§5、実装済み) |

---

## 3. アーキテクチャ

### 3.1 層構造

```
ユーザーコード:  @server.call_tool() など(公式 SDK の API)
─────────────────────────────────────────────
mcp SDK:  lowlevel Server(JSON-RPC / capabilities / ツール実行)
─────────────────────────────────────────────
hayate-mcp:  McpMount(StreamableHTTPTransport)
   POST   → JSON-RPC メッセージ受信 → 単発 JSON 応答 or SSE ストリーム
   GET    → サーバー起点 SSE(resumability は v0.2 で判断)
   DELETE → セッション終了
   Mcp-Session-Id 発行・検証 / Origin 検証
─────────────────────────────────────────────
hayate:  register(app) → app.on("GET"/"POST"/"DELETE", path)
─────────────────────────────────────────────
SessionStore protocol:  memory(既定) | Durable Object(Workers)
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

---

## 4. セッション管理

- `Mcp-Session-Id`(spec 準拠)を発行し、`SessionStore` protocol で保持。
  house style 通り protocol 注入(hayate-auth の Adapter と同型)。
- 既定は in-memory(単一プロセス)。**Workers は Durable Object 実装**
  (本体 `@to_durable_object` を利用)— インスタンス揮発と多重化に耐える唯一の解で、
  Cloudflare の TS 実装(McpAgent)も DO を使う。
- resumability(`Last-Event-ID`)は spec 上 optional。**判断(2026-07-23): v0.2 では非対応** — 再生バッファはセッションが isolate をまたいで生きる DO ストア側に置くのが正しい構造で、メモリストアに足しても本番で意味を成さないため(auth 本番実測でも isolate 揮発を確認)。DO 版と同時に再判断。

## 5. セキュリティ

- **Origin 検証は既定 ON**(spec MUST。`trusted_origins` を指定させる)。
  localhost バインド時の注意も README に明記。
- v0.1 は authless(spec 上 optional)。
- **v0.4: OAuth 2.0 Resource Server 側を実装(出荷済み)**。`McpMount(authorization=Authorization(...))`
  で MCP Authorization(2025-06-18)+ RFC 9728 に対応:
  - `/.well-known/oauth-protected-resource` に Protected Resource Metadata(RFC 9728)を提供
    (`resource` / `authorization_servers` / `bearer_methods_supported` / `scopes_supported`)。
    このエンドポイントはトークン不要の公開ディスカバリ。
  - 未認証リクエストは **401 + `WWW-Authenticate: Bearer resource_metadata="…"`**(RFC 9728 §5.1)
    でメタデータ URL を案内。Claude 等のクライアントはここから AS を発見する。
  - トークン検証は `verify_token(token) -> claims | None` の注入(RFC 6750 Bearer)。
    **AS(トークン発行)側は分離**し、hayate-auth の将来の AS モード、または任意の
    RFC 6749 AS を指せる。これが「MCP サーバー + その AS を 1 アプリに」story の RS 半分。
- ~~**残**: hayate-auth の AS モード(/authorize・/token・動的クライアント登録)は auth 側の
  別機能。揃えば同一アプリに MCP + AS をマウントする完全形になる(証拠駆動)。~~
  **達成(2026-07-23、auth 0.6.0)**: auth の AS モード(DESIGN §19)が出荷され、
  `verify_token=auth.oauth_token_verifier(resource=...)` で完全形が成立。
  公式 SDK クライアントの OAuth フル一周が auth 側 examples/mcp-oauth の CI に常設
  (実測ログ: auth `docs/research/authorization-server.md`)。

## 6. 実行モデル / Workers 制約

- ~~最大リスク: mcp SDK が Pyodide/workerd で import できるかは未検証~~
  **解決(2026-07-22 spike — research/pyodide.md)**: SDK 1.12.4 と全依存
  (pydantic-core は Pyodide wasm wheel)が workerd で import 成功。さらに
  lowlevel `Server` + `ClientSession` を anyio メモリストリームで対向させた
  initialize → tools/list → tools/call の一周が workerd 上で 87 ms で成功。
  **縮退案(最小プロトコル自前実装)は不要 — SDK ブリッジ一本で確定**。
- 残コスト: vendor ~15.4 MiB / Total ~43.5 MiB(4095 modules)。SDK 依存が常に載る
  コスト構造は README に明記する。
- SSE / FFI 境界(proxy lifecycle、`_js_bytes`)は本体 research §5 の知見を継承。

### 6.1 stateless モード(Workers 対応の要、v0.3)

**決定**: `McpMount(stateless=True)` は各リクエストで SDK の
`Server.run(..., stateless=True)` を**そのリクエスト内で await 完結**させる
(`_post_stateless`)。stateless の `ServerSession` は初期化済み扱いのため、
initialize / tools/list / tools/call のどれも単発 JSON-RPC で処理できる。

- **理由**: Workers の bounded なリクエストは、リクエストを跨ぐ detached task
  (`asyncio.ensure_future(server.run(...))`)を許さず isolate を hard-crash させる
  (§11.2、research/workers-do.md)。stateless では run が stream クローズで即完了するため
  detached task が無く、Workers で成立する。**素の `to_workers(app)` で動く(DO 不要)**。
- **却下しなかった代替**: DO でステートフル(§11.2)。サーバー起点ストリームや
  セッション跨ぎ状態が要る場合のみ。証拠駆動で保留。
- 制約: GET(サーバー起点 SSE)は 405、DELETE は no-op 200。ステートフルは ASGI 経路。

### 6.2 リビジョン追従と Workers の SDK 制約(v0.5、2026-07-23)

- **依存フロアは `mcp>=1.12`**(ハード `>=1.28` にしない)。理由:
  - **CPython/ASGI** は最新 SDK 1.28.1 に解決 → `LATEST_PROTOCOL_VERSION = 2025-11-25`。
  - **Workers(Pyodide)** は現状 mcp 1.28 を vendor **できない**: 1.28 が要求する新しい
    pydantic に対応する **pydantic-core の wasm wheel が Pyodide index(0.28.3)に無い**
    (実測: 解決不能)。pywrangler は wasm 互換の最新(mcp 1.12.4、2025-06-18)に解決する。
  - → 「最新準拠」は**ランタイム依存**: CPython は 2025-11-25、Workers は当面 2025-06-18。
    Pyodide が pydantic-core を更新したら Workers も 2025-11-25 に上がる(フロアはそのまま)。
- **`MCP-Protocol-Version` ヘッダ検証**は SDK の `SUPPORTED_PROTOCOL_VERSIONS` を使うので、
  どちらのランタイムでも「そのランタイムの SDK が話せる版」を正しく受理/拒否する(両方で実測)。
- **2026-07-28 RC(stateless core)**: initialize ハンドシェイクと Mcp-Session-Id を transport
  から外す方向。**§6.1 の stateless モードが既にこの形**。RC が stable 化し SDK が追従したら、
  stateless を既定側に寄せる判断をする(証拠駆動、SDK 追従の原則どおり)。

## 7. テスト戦略

- transport 単体は `await mount.fetch(Request(...))` 直叩き(house style の純関数コア)。
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
| ~~SDK が Pyodide で動かない~~ | **解消(2026-07-22 spike)**: import + プロトコル一周を workerd 実機で確認(§6、research/pyodide.md) |
| MCP spec の改訂速度 | transport のみに表面積を絞り SDK 追従。対応リビジョンを README に明記 |
| FastMCP v3 が同領域を埋める | 土俵を変える: Workers + hayate-auth 合流(§5)。汎用 ASGI 統合では競わない |
| PyPI 名スクワット | `hayate-mcp` 空き確認 2026-07-22。0.0.x 早期公開で確保 |

## 10. マイルストーン

| 版 | 内容 | 受け入れ基準 |
|---|---|---|
| ~~**spike**~~ | **完了(2026-07-22)**: SDK import + echo ツールの in-process 一周を workerd で確認 | ✅ research/pyodide.md に記録。§6 は SDK ブリッジ一本で確定 |
| ~~**v0.1**~~ | **完了(2026-07-22)**: McpMount(POST=JSON 単発 / DELETE / GET=405)+ Mcp-Session-Id + memory SessionStore(idle eviction)+ Origin 検証 | ✅ **MCP Inspector CLI から接続し tools/list・tools/call 実行を実測**(uvicorn)。✅ 公式 SDK クライアント(`streamable_http_client` + `ClientSession`)での実 HTTP 一周を E2E テストとして CI に常設。テスト 16。✅ **Claude Code 実機接続も実測(2026-07-23)**: `claude mcp add --transport http` → `claude mcp list` で Connected、ヘッドレス実行で echo ツールの呼び出しに成功。受け入れ基準は両実クライアントで完全達成 |
| v0.2 | **出荷(2026-07-23)**: GET SSE ストリーム(1 本/セッション、409 で多重拒否、close で終端。テスト 20)+ resumability 判断(§4) | GET SSE ✅ |
| v0.3 | **出荷(2026-07-23)**: `stateless=True` モード(§6.1)。**Cloudflare Workers で緑化**(DO 不要) | ✅ **workerd 上で MCP フル一周(initialize → tools/list → tools/call)を curl と MCP Inspector CLI で実測**。テスト 27(stateless 7 追加) |
| v0.4 | **出荷(2026-07-23)**: OAuth 2.0 Resource Server 側(RFC 9728 Protected Resource Metadata + Bearer 検証 + 401/`WWW-Authenticate`)。§5 | ✅ 認可済みクライアントのみ接続可・authless 構成も選択可。テスト 35(authorization 8 追加)。AS 側(トークン発行)は hayate-auth の将来機能 |
| v0.5 | **出荷(2026-07-23)**: 最新 stable **2025-11-25** 準拠(SDK 1.28.1)+ `MCP-Protocol-Version` ヘッダ検証(§2、§6.2) | ✅ CPython は 2025-11-25 ネゴ、Workers は wasm 制約で 2025-06-18(§6.2)。両ランタイムで無効版 400 を実測。テスト 42(protocol-version 7 追加) |
| v1.0 | API 凍結 | 本体 v1.0 より後 |

## 11. Workers 対応(2026-07-23、緑化)

### 11.1 stateless モードで解決(出荷済み)

**`McpMount(stateless=True)` + 素の `to_workers(app)` で Workers 対応が成立**。DO は不要。

- 各リクエストで SDK の `Server.run(..., stateless=True)` を**そのリクエスト内で await 完結**
  させる(`_post_stateless`)。stateless の `ServerSession` は初期化済み扱いなので、
  initialize / tools/list / tools/call のどれも単発で処理できる。
- **detached task が無い**ため、bounded な Workers リクエストに収まる。これが従来の
  DO 案(下記 §11.2)を潰していた根本問題の回避策。
- 制約: サーバー起点メッセージ(GET SSE)とセッション跨ぎ状態は持てない。GET は 405、
  DELETE は no-op 200。ステートフルが要るツールは ASGI(examples/echo)を使う。

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
| 依存 | `hayate` + `mcp`(公式 SDK)。それ以外は追加しない |
