# MCP SDK on workerd spike(DESIGN §10 spike)

> 2026-07-22 実施。spike コードは `spike/sdk-workers/`(使い捨て、entry.py)。
> 結論は DESIGN §6 / §9 に反映済み。

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
- Workers 本番(deploy)でのバンドル制限・コールドスタート・DO SessionStore(v0.2)。
- FastMCP 定義ツールの SDK `Server` への変換受け入れ(DESIGN §3.2 の要検証項目)。
