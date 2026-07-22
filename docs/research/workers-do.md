# Workers Durable Object session store — verification log (v0.2, in progress)

> 2026-07-23。ローカル workerd で **未達**の状態を正直に記録する。
> 本体 research §5 と同じ規律: 実機で緑になるまでチェックしない。

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

### 未解決のブロッカー

- `binding.get(binding.newUniqueId())` / `binding.get(binding.idFromString(id))` /
  `binding.getByName(name)` の**いずれでも** DO サブリクエストが
  `pyodide.http.AbortError: internal error; reference=...` になる。
  Python の DO 側トレースは一切出ない(= workerd が Python 実行前に dispatch を失敗させている)。
- POST ボディを bytes に読み直して再構築するルート(`stub.fetch(url, method, headers, body)`)、
  元リクエスト素通し(`forward`)、`js.Request.new(raw, {headers})` 再構築 —
  3 通りとも同じ `internal error`。ボディ再生の問題ではない。
- 本体 research §5 で検証済みの DO は **GET forward**(`forward(c, getByName(name))`)。
  **POST ボディを伴う DO サブリクエスト**は本体側で未検証の経路であり、ここが疑わしい。
  切り分けには最小 DO(counter 相当)への POST を本体側で先に緑化するのが筋。

## 次アクション(v0.2 継続)

1. 本体 `examples/workers` の DO に POST ルートを足し、`forward` / 明示サブリクエストの
   どちらで POST が通るかを本体側で最小再現・緑化する(hayate 本体の research §5 に追記)。
2. 緑化した経路を hayate-mcp の `route_to_session` に反映。
3. その上で MCP Inspector / SDK クライアントから wss/https 接続を実測(v0.2 受け入れ基準)。

**判断**: v0.2 は「GET SSE ストリーム(CPython で検証済み・テスト常設)」を確定分として出荷し、
Workers + DO は本ログの未達を明記したまま継続。実機で緑になっていないものはチェックしない。
