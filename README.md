# agentic-video-analysis-skill

動画をフレームタイル化し、`aitool recognize-image` で反復的に解析する Agent Skill です。AIエージェントが「粗く見て、気になった所だけ細かく見る」方式で見どころ候補を抽出します。

## できること

- 動画全体の概要把握と見どころ候補の列挙
- 候補範囲の中fps確認による事実の確定（仮説注入・JSON構造化出力）
- 文字・細部が読めない箇所の特定時刻フル解像度ズーム確認（任意）
- 境界が曖昧な箇所の高fps精密確認（任意）
- 後段の動画構成エージェントへ渡すための `final.md` 出力

ゴールは**見どころ候補の抽出**です。

## インストール

使用するAIエージェントに合わせて設定してください。

`npx skills add` を使用する場合、以下のコマンドで導入できます。

```bash
npx skills add Nu424/agentic-video-analysis-skill
```

## 前提環境

スキル内のスクリプトを実行するには、以下が必要です。

| 項目 | 用途 |
| --- | --- |
| `ffmpeg` / `ffprobe` | 動画からフレーム抽出 |
| Python 3 + `Pillow` | タイル画像の生成 |
| `aitool` + `OPENROUTER_API_KEY` | タイル画像の解析 |

依存のインストール:

```bash
python -m pip install -r .agents/skills/agentic-video-analysis-skill/scripts/requirements.txt
```

`aitool` が未導入の場合:

```bash
uv tool install git+https://github.com/Nu424/aitool-iroiro.git
```

aitool が要求する `OPENROUTER_API_KEY` は `.env` または `~/.env.global` に設定してください。

## 使い方

### 1. エージェントに動画解析を依頼する

スキルをインストールしたエージェントに、解析したい動画のパスと目的を伝えます。

例:

> `video.mp4` を解析して、見どころ候補を抽出してください。

エージェントはスキル（`SKILL.md`）の手順に従い、タイル化・解析・候補の絞り込みを反復します。ユーザーが細かいコマンドを打つ必要はありません。

### 2. 成果物を確認する

1回の解析は次のディレクトリにまとめられます。

```text
output/agentic_sessions/<video_stem>_<timestamp>/
  overview/      # 全体把握のタイルと解析結果
  candidates/    # 候補範囲のタイル・config・解析結果
  zooms/         # ズーム（特定時刻フル解像度）の確認（任意）
  refinements/   # 精密確認（任意）
  notes/         # 候補一覧などの中間メモ
  final.md       # 最終まとめ
```

`final.md` に、確定した見どころ候補・根拠・タイムライン要約がまとまります。

### 3. 手動でスクリプトを試す（任意）

エージェント経由ではなく直接試す場合の例です。

全体把握（低fps）:

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py \
  --video video.mp4 --fps 0.5 \
  --output output/agentic_sessions/example/overview/full.jpg

python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py \
  --manifest output/agentic_sessions/example/overview/full/manifest.json \
  --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt \
  --output output/agentic_sessions/example/overview/full_analysis.txt
```

候補範囲の一括タイル化・解析は `examples/ranges.example.json` を雛形に config を作成し、`--config` / `--summary` オプションを使います。詳細は [SKILL.md](skills/agentic-video-analysis-skill/SKILL.md) を参照してください。

## 仕組み

動画全体を高fpsで一括判断するのではなく、**低密度の全体観察から始め、重要そうな範囲だけを再タイル化しながら解析**します。

使うスクリプトは2本です。

| 役割 | スクリプト |
| --- | --- |
| タイル化（単一範囲 / config複数範囲 / zoom） | `scripts/tile_video_frames.py` |
| 解析（単一 / 複数manifest / summary一括 / 並列 / 分割） | `scripts/analyze_tile_manifest.py` |

各フレームには `F<index> t=<秒>s` のラベル（600秒超は `m:ss.s`）が付き、解析結果はセルラベルを根拠に記述します。manifest 内の全タイルは **原則1回の API 呼び出し**でまとめて渡されます（`--max-tiles-per-call` 超過時のみ時系列で分割）。共通処理は `scripts/common.py` に集約しています。

## 実行の流れ

```text
Step 1 全体把握    動画全体を低fps(0.5)でタイル化 → overview解析 → 候補を多めに列挙
        │
Step 2 候補確認    候補をconfig化 → 一括タイル化(中fps) → detail解析 → 事実を確定
        │
Step 2.5 ズーム    文字・細部が読めない候補だけ特定時刻をフル解像度で確認 → zoom解析（任意）
        │
Step 3 精密確認    境界が曖昧な候補だけ高fpsで再タイル化 → refine解析（任意）
        │
Step 4 最終出力    確定した見どころ候補だけを final.md にまとめる
```

各ステップは **「① タイル化 → ② 解析 → ③ 判断」** の繰り返しです。

### fps の目安

| 段階 | fps | 目的 |
| --- | --- | --- |
| 長尺の章立て（10分超） | 0.1〜0.2 | 大きな区切りの把握 |
| Step 1 全体把握 | 0.5〜1 | 概要と候補列挙 |
| Step 2 候補確認 | 3〜5 | 出来事の存在確認 |
| Step 2.5 ズーム | 特定時刻のみ・フル解像度 | 文字・細部の判読 |
| Step 3 精密確認 | 8〜10 | 開始・終了秒の詰め |

### 原則

- **偽陰性 < 偽陽性** — 全体把握では候補を多めに挙げ、取りこぼしを避ける
- **映像上の根拠だけで書く** — 見えていないものは補完しない
- **候補に含めなかった出来事は最終出力に書かない**

## リポジトリ構成

```text
skills/agentic-video-analysis-skill/
  SKILL.md              # エージェント向け手順書
  scripts/              # common.py + タイル化・解析CLI
  prompts/              # overview / detail / zoom / refine プロンプト
  examples/             # 候補範囲定義の雛形
```

## 詳細ドキュメント

エージェントが参照する完全な手順・パラメータ一覧は [skills/agentic-video-analysis-skill/SKILL.md](skills/agentic-video-analysis-skill/SKILL.md) にあります。
