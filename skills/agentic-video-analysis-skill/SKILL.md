---
name: agentic-video-analysis-skill
description: |
  動画をフレームタイル化し、aitool recognize-imageで反復的に解析するためのスキル。
  動画解析、見どころ抽出、ハイライト候補探索、ゲーム実況分析、エージェンティックな動画理解を行うときに使用する。
---

# Agentic Video Analysis

動画全体を固定fpsで一括判断するのではなく、**低密度の全体観察から始め、重要そうな範囲だけを絞って再タイル化しながら解析する**スキル。

## 原則

- **粗く見て、気になった所だけ細かく見る**。全編を高fpsでタイル化しない。
- **偽陰性 < 偽陽性**。全体把握では候補を多めに挙げる。取りこぼしの方が悪い。
- **映像上の根拠だけで書く**。セルラベル `t=<秒>s`（動画内の絶対時刻）を必ず根拠に含める。見えていないものは補完しない。
- **このスキルのゴールは「見どころ候補の抽出」**。採用/不採用の最終判断はしない（後段の動画構成エージェントに渡す）。

## 全体の流れ

```text
Step 1 全体把握    動画全体を低fps(0.5)でタイル化 → overview解析 → 候補を多めに列挙
        │
Step 2 候補確認    候補をconfig化 → 一括タイル化(中fps) → detail解析 → 事実を確定
        │
Step 3 精密確認    境界が曖昧な候補だけ高fpsで再タイル化 → refine解析（任意）
        │
Step 4 最終出力    確定した見どころ候補だけをまとめる
```

各ステップは **「① タイル化 → ② 解析 → ③ 判断」** の繰り返し。使うスクリプトは2本だけ。


| 役割                              | スクリプト                              |
| ------------------------------- | ---------------------------------- |
| タイル化（単一範囲 / config複数範囲）         | `scripts/tile_video_frames.py`     |
| 解析（単一 / 複数manifest / summary一括） | `scripts/analyze_tile_manifest.py` |


> コマンド例は1行で記載している。PowerShell・Bashどちらでもそのまま実行できる。

---

## セットアップ

依存は `ffmpeg` / `ffprobe`（PATH上）と `Pillow`、解析に `aitool` と `OPENROUTER_API_KEY`。

```bash
python -m pip install -r .agents/skills/agentic-video-analysis-skill/scripts/requirements.txt
```

ローカルに入れたくない場合は `uv` で一時実行してよい（以降の `python ...` を `uv run --with Pillow python ...` に読み替える）。

```bash
uv run --with Pillow python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --help
```

`aitool recognize-image` を使うため、未設定なら `.env` または `~/.env.global` に `OPENROUTER_API_KEY` を設定する。`aitool` が無ければ `uv tool install git+https://github.com/Nu424/aitool-iroiro.git` などで導入する。

## セッション構成

1回の解析は次のディレクトリにまとめる。`<timestamp>` は実行時刻。

```text
output/agentic_sessions/<video_stem>_<timestamp>/
  overview/      # 全体把握タイルと解析結果
  candidates/    # 候補範囲のタイル・config・解析結果・batch_summary.json
  refinements/   # 精密確認タイルと解析結果
  notes/         # 候補一覧などの中間メモ
  final.md       # 最終まとめ
```

中間結果はテキストだけで保持せず、各範囲の解析出力を必ずファイルに残す。

---

## Step 1: 全体把握

**目的**: 動画の概要を掴み、見どころになりそうな範囲を多めに列挙する。

### ① タイル化（全体を fps=0.5）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --fps 0.5 --output output/agentic_sessions/example/overview/full.jpg
```

- `--start` / `--end` 省略で動画全体。フレームが多ければ自動で複数タイル（`tile_000.jpg`…）に分割され、`manifest.json` が書かれる。
- まず **fps=0.5** を試す。候補列挙が目的ならこれで十分なことが多い。

### ② 解析（overviewプロンプト）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --manifest output/agentic_sessions/example/overview/full/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt --output output/agentic_sessions/example/overview/full_analysis.txt
```

manifest内の全タイルが**1回のAPI呼び出し**でまとめて渡される（タイルごとに呼ばない）。

### ③ 判断

解析結果から候補を `notes/candidates.md` に列挙する。各候補に `priority` と `needs_followup` を付ける。

```markdown
- 38-46s: priority=high, needs_followup=yes, reason=大きな画面変化
- 0-12s:  priority=low,  needs_followup=no,  reason=導入部
```

→ `priority=high` または `needs_followup=yes` の候補を Step 2 へ。

---

## Step 2: 候補確認

**目的**: 候補範囲を中fpsで見て、何が起きているかの事実を確定する。

### ① タイル化（候補をconfigで一括）

候補を範囲定義JSONにまとめる。雛形: [examples/ranges.example.json](examples/ranges.example.json)

```json
{
  "video": "video.mp4",
  "output_dir": "output/agentic_sessions/example/candidates",
  "defaults": { "fps": 5, "pad": 2, "frames_per_tile": 12 },
  "ranges": [
    { "label": "candidate_a", "start": 38, "end": 46, "priority": "high" },
    { "label": "candidate_b", "start": 49, "end": 53.5, "priority": "high" }
  ],
  "summary_output": "output/agentic_sessions/example/candidates/batch_summary.json"
}
```

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --config output/agentic_sessions/example/candidates/ranges.json --merge-overlaps
```

- `defaults` が全rangeの既定値、各 range で上書きできる。
- `--merge-overlaps` で重なる範囲をマージ（しきい値は `--overlap-threshold`、既定0.5）。
- `--dry-run` で処理予定の範囲だけ確認できる。
- range ごとにタイル＋`manifest.json` を出力し、全rangeの `manifest_path` を含む `batch_summary.json` を書く。

### ② 解析（detailプロンプト・summaryで一括）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --summary output/agentic_sessions/example/candidates/batch_summary.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt
```

- `--summary` で全rangeのmanifestを自動列挙し、**range ごとに1回ずつ**解析する。
- 出力は省略時 manifest 横に `<range名>_analysis.txt` で自動命名（`--output-dir` でまとめ先を指定可）。

### ③ 判断

確認できた事実を `notes/candidates.md` に反映する。さらに見たい箇所が出たら候補を足してよい（1反復で追加は最大3件程度）。`confidence: low` や境界がタイル端にかかる候補は Step 3 へ。

---

## Step 3: 精密確認（任意）

**目的**: 出来事の開始秒・終了秒を詰める。境界が曖昧な候補だけ行う。

### ① タイル化（短い範囲を高fps）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --start 38 --end 46 --pad 2 --fps 9 --output output/agentic_sessions/example/refinements/range_38_46_fps9.jpg
```

1範囲は **おおよそ10秒以内**。長ければ分割する。

### ② 解析（refineプロンプト）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --manifest output/agentic_sessions/example/refinements/range_38_46_fps9/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/refine.txt --output output/agentic_sessions/example/refinements/range_38_46_fps9_analysis.txt
```

---

## Step 4: 最終出力

確定した見どころ候補だけをまとめる。ユーザー指定がなければ以下の形式。

```markdown
## 概要
[動画全体の要約]

## 見どころ候補
### 1. [タイトル] ([start_sec]s - [end_sec]s)
- **title**:
- **start_sec** / **end_sec**:
- **summary**:
- **根拠**: [解析ファイル名、セルラベル、見えている事実]

## 追加確認した範囲
- [start_sec]-[end_sec], fps=[n], manifest=[path], analysis=[path]

## タイムライン要約
[見どころ候補だけを時系列順に並べた簡潔な一覧]

## 注意点
[誤認しやすい点、音声未確認など]
```

**タイムライン要約は確定した「見どころ候補」だけから作る**。各タイルの生出力や未統合の中間メモを流し込まない。候補に含めなかった出来事は書かない。固有名称・細部は候補の根拠に基づくものだけを書く。

---

## パラメータ早見表

### 3段階のfps戦略


| 段階          | fps   | 目的        |
| ----------- | ----- | --------- |
| Step 1 全体把握 | 0.5〜1 | 概要と候補列挙   |
| Step 2 候補確認 | 3〜5   | 出来事の存在確認  |
| Step 3 精密確認 | 8〜10  | 開始・終了秒の詰め |


### タイル設計

- 1タイルあたり **12〜16枚** を目安（`--frames-per-tile`、既定12）。超えると自動分割される。
- 候補・精密確認では前後 **2〜3秒** のパディング（`--pad`）を付け、境界の出来事を取りこぼさない。
- 精密確認の1範囲は **おおよそ10秒以内**。

### 再タイル化する / しない


| する                                         | しない                       |
| ------------------------------------------ | ------------------------- |
| `priority=high` / `needs_followup=yes` の候補 | `priority=low` で追加確認不要な区間 |
| 画面構成・状態が大きく変わる箇所                           | 変化の乏しい導入部・待機部・説明部         |
| 出来事の開始・終了がタイル端にかかる                         | すでに十分な根拠が取れている候補          |
| モデルが `confidence: low` と判断した箇所             | —                         |


- 1回の反復で追加する候補は **最大3件** 程度。
- 既存の詳細範囲と **50%以上重なる** 候補は、新規追加せず既存範囲へマージ（`--merge-overlaps`）。

---

## スクリプトリファレンス

### tile_video_frames.py

各セルに `F<index> t=<秒>s` のラベルが付く。`t=<秒>s` は動画内の絶対時刻。

**単一範囲モード**（`--video` 必須）:

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --start 38 --end 46 --pad 2 --fps 5 --output output/agentic_sessions/example/candidates/range_38_46_fps5.jpg
```

**configモード**（`--config` 指定時は `--start/--end` 等を無視）:

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --config path/to/ranges.json --merge-overlaps
```


| オプション                                      | 説明                                          |
| ------------------------------------------ | ------------------------------------------- |
| `--video` / `-v`                           | 入力動画パス（単一範囲モードで必須）                          |
| `--config` / `-c`                          | 範囲定義JSON。複数範囲を一括処理                          |
| `--start` / `--end`                        | 開始秒 / 終了秒。省略時は0秒 / 動画末尾                     |
| `--pad`                                    | 開始・終了の前後に足す秒数                               |
| `--fps`                                    | 1秒あたりの抽出枚数                                  |
| `--frames-per-tile` / `-f`                 | 1タイルあたりの最大フレーム数（推奨12〜16）                    |
| `--width`                                  | ffmpeg抽出時のリサイズ幅px                           |
| `--tile-width` / `--tile-height`           | タイル画像の目標サイズ                                 |
| `--output` / `-o`                          | 出力パス。複数タイル時は同名ディレクトリに `tile_000.jpg` 等を出力   |
| `--metadata-output`                        | manifest JSON の出力先。省略時は出力先の `manifest.json` |
| `--merge-overlaps` / `--overlap-threshold` | configモードで重なる範囲をマージ（既定しきい値0.5）              |
| `--dry-run`                                | configモードで処理予定の範囲を表示するだけ                    |


configのrangeキー（`label`, `start`, `end`, `fps`, `pad`, `frames_per_tile`, `width` …）は対応する単一範囲オプションと同名。`defaults` に共通値、各 range で上書き。`output_dir` と `label` から出力名が決まる。

### analyze_tile_manifest.py

manifest内の全タイルを **1回のAPI呼び出し** で `aitool recognize-image` に渡す。複数manifestはmanifestごとに1回ずつ呼ぶ（「1範囲=1呼び出し」を維持）。モデル既定は `google/gemini-3.5-flash`。

```bash
# 単一
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --manifest path/to/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt --output path/to/analysis.txt

# 複数（直接指定）
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --manifest a/manifest.json b/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt

# 複数（タイル化summaryから自動列挙）
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --summary path/to/batch_summary.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt
```


| オプション               | 説明                                                   |
| ------------------- | ---------------------------------------------------- |
| `--manifest` / `-m` | manifest.json のパス（複数指定可）                             |
| `--summary` / `-s`  | `batch_summary.json`。`results[].manifest_path` を自動列挙 |
| `--prompt` / `-p`   | プロンプトテキストファイル（バッチ内は共通）                               |
| `--output` / `-o`   | 出力パス（manifestが1件のときのみ）。省略時はmanifest横に自動命名            |
| `--output-dir`      | 複数manifestの出力をまとめるディレクトリ                             |
| `--model`           | 使用モデル（既定 `google/gemini-3.5-flash`）                  |
| `--dry-run`         | 実行コマンドを表示するだけ                                        |


### プロンプトテンプレート

- 全体把握: [prompts/overview.txt](prompts/overview.txt)
- 候補確認: [prompts/detail.txt](prompts/detail.txt)
- 精密確認: [prompts/refine.txt](prompts/refine.txt)

境界が曖昧な候補だけ、必要に応じて別モデルで再確認してもよいが、通常は `google/gemini-3.5-flash` だけでよい。

---

## 並列化

複数候補のタイル化・解析など独立した処理は、CLIやサブエージェントで並列実行してよい。`tile_video_frames.py --config` は複数範囲を逐次処理するため、さらに速度が必要なら config を分割して並列に起動する。低密度で十分な区間まで高fpsで全編を再タイル化しない。

## 中間成果物

各ステップで最低限これを残す。

- タイル画像と `manifest.json`
- 範囲ごとの `*_analysis.txt`（`analyze_tile_manifest.py` の出力）
- configモードの `batch_summary.json`
- `notes/candidates.md`（候補一覧メモ）

