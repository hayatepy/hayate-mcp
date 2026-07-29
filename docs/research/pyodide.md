# MCP SDK on workerd spike(DESIGN §10 spike)

> 2026-07-22 実施。spike コードは `spike/sdk-workers/`(使い捨て、entry.py)。
> 以下は当時の実測記録。2026-07-24 の v0.8 判断で結論を更新した。

## 2026-07-30 更新

Workers-native runtimeはMCP 2026-07-28のhandshake-free discovery/toolsと
2025-11-25 compatibilityを同じendpointで処理する。公式SDK自体は引き続き
Workers bundleへ入れず、CPython側はSDK 2.xへ更新した。workerd E2EもSDK 2.x
clientで検証する。以下の1.28.1/2025記述はv0.8判断時の履歴である。

## 2026-07-24 更新

SDK 1.12.4 自体は動作したが、MCP 2025-11-25 対応の SDK 1.28.1 は
Pydantic 2.11+ を要求し、pywrangler 1.15 が解決に使う Pyodide 0.28.3
index の Pydantic 2.10.6 / pydantic-core 2.27.2 では導入できない。
Workers だけ 2025-06-18 に留める判断を撤回し、v0.8 で
`WorkerMcpServer` / `WorkerMcpMount` を実装した。

- 対応 surface は capability と一致する lifecycle / ping / tools。
- Emscripten bundle から `mcp` / Pydantic を除去。
- `jsonschema 4.25.1` と Pyodide wasm wheel の `rpds-py 0.23.1` で
  Draft 2020-12 input/output schema を検証。
- workerd 上で MCP 2025-11-25 initialize → tools/list → tools/call を通し、
  公式 SDK 1.28.1 の `ClientSession` からの相互運用も確認。
- CI の `scripts/check_workerd.sh` が local wheel build、bundle dependency、
  workerd 起動、公式 client 一周を再現する。

以下の SDK 1.12.4 の測定値は、旧方式との比較資料として保持する。

## 結論(TL;DR)

1. **mcp SDK(公式 python-sdk 1.12.4)は workerd の Pyodide 3.13.2 で動く。§6 の縮退案
   (Workers 版のみ最小プロトコル自前実装)は不要 — SDK ブリッジ一本で確定**。
2. import probe: `mcp` / `mcp.types` / `mcp.server` / `mcp.server.lowlevel` /
   `mcp.client.session` / `mcp.shared.memory` と依存
   (anyio / pydantic / **pydantic_core(Pyodide wasm wheel)** / pydantic_settings /
   httpx / httpx_sse / jsonschema / starlette / sse_starlette)**すべて import 成功**。
   uvicorn だけ環境マーカーで vendor から外れる(workerd 自身がサーバーなので不要・無害)。
3. **プロトコル一周も workerd 上で成功**: lowlevel `Server` + `ClientSession` を
   `mcp.shared.memory.create_connected_server_and_client_session`(anyio メモリストリーム +
   task group)で対向させ、initialize → tools/list → tools/call(echo)まで 87 ms。
   anyio の task group / メモリストリームが Pyodide の WebLoop 上で実際に動くことの実証
   = hayate-mcp transport が SDK に接続する縫い目そのもの。

## 実測メモ

- 環境: ローカル workerd(workers-py 1.15 / wrangler 4.113 / compatibility_date 2026-07-01)。
- vendor 解決: pywrangler の resolve は Pyodide 公式インデックス
  (cdn.jsdelivr.net/pyodide/v0.28.3)を extra-index に使い、22 パッケージが解決される。
  pydantic-core は `pydantic_core-2.27.2-cp313-cp313-pyodide_2025_0_wasm32.whl` が選ばれる。
- バンドルサイズ: Vendored Modules ~15.4 MiB / Total(4095 modules)~43.5 MiB。
  デプロイサイズ制限との関係は v0.2(Workers 対応)で要確認。ツール定義だけの
  ユーザーには SDK 依存ぶんが常に載る、というコスト構造は README に明記する。
- リクエストごとに Server + セッションを組み立てて一周 87 ms(初回リクエスト、
  import 込みの可能性あり)。実 transport ではセッションを SessionStore で保持するため
  この経路のホットパス化はしない。

## Windows での再現手順

hayate-auth `docs/research/kdf.md` の回避手順と同じ。ただし依存に wasm wheel
(pydantic-core)が含まれるため、手動 vendor は `--python-platform wasm32-pyodide2025
--python-version 3.13` を付ける:

```
uv pip install --python .venv --python-platform wasm32-pyodide2025 --python-version 3.13 \
  --target python_modules --no-build -r pylock.toml --preview-features pylock
printf '1.15.0' > python_modules/.synced && printf '1.15.0' > .venv-workers/.synced
uv run pywrangler dev   # UV_PYTHON_DOWNLOADS=automatic UV_PYTHON_PREFERENCE=managed を前置
```

(.synced は空 touch では不可 — workers-py バージョン文字列が入っていないと
sync が再実行されて失敗する。)

## 未検証(次の判断点)

- Streamable HTTP transport 実装後の MCP Inspector / Claude Code 実クライアント接続(v0.1 受け入れ基準)。
- ~~Workers 本番(deploy)でのバンドル制限・コールドスタート・DO SessionStore(v0.2)。~~
  **確定(2026-07-23、hayate-auth AS spike の本番実測 — 同日 Paid で決着)**:
  バンドルサイズは gzip 後 3 MiB 内で Free でも upload は通る。
  **遅延(リクエスト時)import + Workers Paid が本番の正解形** — Python Workers の
  ランタイム CPU リミッター(`introspection.CpuLimitExceeded`)はプラン準拠で、
  Free(~2 s)では mcp チェーンの import が死ぬが **Paid は既定予算のまま完走**
  (cold で import ~3 s CPU、PRM cold 実測 3.6〜7.4 s)。グローバル import は
  deploy validator の `Top-level await in module is unsettled`(startup 予算、
  プラン非依存)で不可のまま。本番フル一周(AS フロー → Bearer initialize →
  Inspector CLI tools/call)は hayate-auth research/authorization-server.md §5.5。
- FastMCP 定義ツールの SDK `Server` への変換受け入れ(DESIGN §3.2 の要検証項目)。
