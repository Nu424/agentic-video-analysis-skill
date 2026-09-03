"""avs: agentic-video-analysis-skill の実装パッケージ。

CLI（`scripts/*.py`）は引数解釈だけを行い、処理本体はこのパッケージが持つ。

- `common`   : ffprobe / JSON IO / タイムスタンプ整形 / フォント / UTF-8 出力
- `tiling`   : フレーム抽出・グリッド計算・タイル描画・manifest 構築・zoom 抽出
- `ranges`   : config 読み込み・defaults 合成・重なりマージ・範囲ごとのオプション生成
- `prompts`  : プロンプト組み立て（目的・タイル読解・仮説・追加コンテキスト）
- `analysis` : manifest 解析（チャンク分割・JSON 抽出・パート統合・aitool 実行）
"""

__all__ = ["analysis", "common", "prompts", "ranges", "tiling"]
