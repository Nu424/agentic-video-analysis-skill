# agentic-video-analysis-skill

動画を **2段階で反復解析**して、見どころ候補を網羅的に抽出する Agent Skill です。

全体を低fpsで把握してから、動画の**全区間**を短い範囲に割って高密度で見直します。
解析は OpenRouter（フレームタイル画像）または Gemini API（ネイティブ動画クリップ・音声）で行い、
結果は検証・統合されて `final.md` と `timeline.json` になります。

## できること

- 動画全体の把握と、全区間をカバーする範囲計画（隙間と末尾を取りこぼさない）
- 範囲ごとの高密度な悉皆列挙（仮説注入・JSON構造化出力・範囲ごとの失敗隔離とレジューム）
- 誤認・根拠なし・境界・低確信の自動フラグ付け（出来事は削除せずフラグだけ付ける）
- 重複統合と LLM 統合による `timeline.json` / `final.md` の生成
- 文字や細部のズーム確認、クロップ拡大、開始・終了秒の精密確認
- 複数実行の和集合による網羅率の底上げ、音声解析（Gemini）
- 全 LLM 呼び出しのプロンプト・usage・コストの記録（`usage.jsonl`）

ゴールは**見どころ候補の抽出**です。採用/不採用の最終判断はしません。

## インストール

使用するAIエージェントに合わせて設定してください。`npx skills add` を使う場合:

```bash
npx skills add Nu424/agentic-video-analysis-skill
```

## 前提環境

| 項目 | 用途 | 必須か |
| --- | --- | --- |
| `ffmpeg` / `ffprobe` | フレーム抽出・動画長の取得 | タイル経路で必須（ネイティブ経路でも `ffprobe` は使用） |
| Python 3.11+ と `Pillow` | タイル画像の生成 | タイル経路で必須 |
| `OPENROUTER_API_KEY` | 既定バックエンド（OpenRouter を直接呼び出し） | `--backend openrouter` で必須 |
| `GEMINI_API_KEY` と `google-genai` | Gemini バックエンド（動画クリップ・音声） | `--backend gemini` で必須 |

| `--backend` | 必要なキー | 追加パッケージ | 既定モデル |
| --- | --- | --- | --- |
| `openrouter`（既定） | `OPENROUTER_API_KEY` | 不要 | `google/gemini-3.7-flash` |
| `gemini` | `GEMINI_API_KEY` | `google-genai` | `gemini-3.7-flash` |

キーは 環境変数 → カレントの `.env` → `~/.env.global` の順に探します。

```bash
python -m pip install -r .agents/skills/agentic-video-analysis-skill/scripts/requirements.txt
```

`uv` があればインストール不要で実行できます（`python ...` を `uv run --with Pillow python ...` に読み替え）。

## 使い方

### 1. エージェントに頼む

スキルを入れたエージェントに、動画のパスと目的を伝えるだけです。

> `video.mp4` を解析して、見どころ候補を抽出してください。

エージェントは `SKILL.md` の手順に従って標準経路を実行し、結果のフラグを見て追加の確認（ズーム・精密確認・範囲追加）を判断します。
動画のジャンルや「ここを見てほしい」という観点を一緒に伝えると、エージェントがドメイン定義（`domain.json`）を作って精度を上げられます。

### 2. 直接実行する

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/run_pipeline.py --video video.mp4 --objective "動画の見どころ候補の抽出"
```

これだけで、全体把握 → 範囲計画 → タイル化 → 詳細解析 → 検証 → 統合 → レポートまで走ります。
実行前に規模とコストを見るなら `--dry-run`、Gemini のネイティブ動画理解を使うなら `--backend gemini --input native` を付けます。
各ステップは出力が既にあればスキップするので、途中で止まっても同じコマンドで再開できます。

### 3. 出力を見る

```text
output/agentic_sessions/<video_stem>_<timestamp>/
  session.json      実行条件と各ステップの記録
  usage.jsonl       LLM呼び出しごとの usage / コスト
  overview/         全体把握のタイルと解析結果
  ranges/           範囲計画・範囲ごとのタイル・解析結果・検証結果
  zooms/            ズーム確認（任意）
  refinements/      精密確認（任意）
  merge/            機械統合・最終タイムライン・検証レポート
  final.md          最終まとめ
```

まず `final.md` を読みます。「見どころ候補」「タイムライン要約」「注意点」がまとまっています。
根拠が怪しい項目は `merge/validation_report.json` のフラグを見て、タイル画像で該当セルを目視できます。
コストと「次にやること」は実行の最後に表示され、あとから `session_report.py --session <session>` でも出せます。

## 仕組み

精度を決めるのは入力形式でもモデルでもなく、**反復構造**です。

```text
全体把握（低fps・全編）
    ↓ 候補 + 隙間 + 末尾
全区間カバーの範囲計画（1範囲 ≤ 8秒）
    ↓ 範囲ごとに1呼び出し
詳細解析（高fps・悉皆列挙・仮説の確認と反証）
    ↓
検証（誤認・根拠なし・境界・低確信にフラグ）
    ↓
統合（重複排除 → LLM統合）→ final.md
```

全編を一度に解析すると、動画の後半と場面の境界を中心に大量に取りこぼします。
範囲を割って高密度で見直すと、それが大きく改善します。先行検証で、この差が入力形式やモデル世代の違いより大きいことを確認しています。
候補の付いた範囲だけを見るのでは足りず、**隙間と末尾まで含めて全区間を覆う**ことが効きます。

### タイル と ネイティブ の選び方

| 入力形式 | 特徴 | 向いている場面 |
| --- | --- | --- |
| `--input tile`（既定） | フレームをタイル画像にして送る。安く、タイル画像とセルラベルが残るので**人間が根拠を確認できる** | 迷ったらこちら |
| `--input native`（`--backend gemini` 限定） | 動画クリップをそのまま送る。実行ごとのばらつきが小さく、**音声も扱える** | ffmpeg が使えない環境、音声も見たい場合 |

網羅率の差は測定のばらつきの範囲なので、精度ではなく用途で選んで構いません。

## リポジトリ構成

```text
skills/agentic-video-analysis-skill/   # npx skills add で配布される単位
  SKILL.md                             # エージェント向け手順書
  prompts/                             # chapters / overview / detail / zoom / refine / merge / audio
  examples/                            # 範囲定義とドメイン定義の雛形
  scripts/                             # 8本のCLI + avs/ パッケージ（実装本体）
eval/                                  # 評価基盤（正解データ照合・網羅率・和集合・コスト）
tests/                                 # pytest。合成動画とモックバックエンドでAPI不要
docs/
  WORKFLOW.md                          # 分析ワークフローの設計文書
  WORKPLAN.md / WORKPLAN-v2.md         # 改修作業書（履歴）
```

CLI は引数解釈だけを行い、処理本体は `scripts/avs/`（`common` / `session` / `tiling` / `ranges` / `prompts` /
`analysis` / `validate` / `merge` / `cost` / `pipeline` / `backends`）にあります。

## 評価

出力を眺めるだけでは良し悪しは分かりません。プロンプト・既定値・モデルを変えるときは、
人手で作った正解データに対して網羅率・誤認数・コストを測ります。手順とツールは [eval/README.md](eval/README.md) にあります。

```bash
python eval/score.py <session>/merge/timeline.json --gt eval/fixtures/<name>/ground_truth.json --session <session>
python eval/union_recall.py --gt eval/fixtures/<name>/ground_truth.json run1/timeline.json run2/timeline.json
```

## v1 からの変更点

- **`aitool` 依存を廃止**。OpenRouter を直接呼び出すか、Gemini API を使います。`aitool` のインストールは不要になりました。
- **`analyze_tile_manifest.py` → `analyze.py` に改名**。ネイティブ動画クリップ入力（`--ranges` / `--video --start --end`）に対応しました。
- **既定モデルを `google/gemini-3.7-flash` に変更**（測定に基づく。世代を上げても差はばらつきに埋もれ、誤認だけ増えました）。
- **JSON検証が既定 ON**（`--expect-json` は廃止、無効化は `--raw`）。
- 全区間カバーの範囲計画、後段バリデーション、統合と `final.md` 生成、音声解析、標準ドライバ（`run_pipeline.py`）を追加しました。
- 詳細は [SKILL.md](skills/agentic-video-analysis-skill/SKILL.md) と [docs/WORKFLOW.md](docs/WORKFLOW.md) を参照してください。
