"""avs: agentic-video-analysis-skill の実装パッケージ。

CLI（`scripts/*.py`）は引数解釈だけを行い、処理本体はこのパッケージが持つ。

- `common`   : ffprobe / JSON IO / タイムスタンプ整形 / フォント / UTF-8 出力
- `tiling`   : フレーム抽出・グリッド計算・タイル描画・manifest 構築・zoom 抽出
- `ranges`   : config 読み込み・defaults 合成・重なりマージ・範囲ごとのオプション生成
- `prompts`  : プロンプト組み立て（目的・タイル読解・仮説・追加コンテキスト）
- `analysis` : 解析本体（チャンク分割・JSON 抽出・パート統合・監査記録）
- `validate` : 後段バリデーション（フラグ付与・confidence 調整・フラグ集計）
- `merge`    : 機械統合・LLM 統合・和集合・音声取り込み・final.md 生成
- `backends` : LLM バックエンド（openrouter / gemini）
- `cost`     : usage 正規化・単価表・コスト概算
- `session`  : セッションディレクトリ（session.json / usage.jsonl）とレポート集計
"""
