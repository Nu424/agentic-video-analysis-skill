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
- **解析目的は `--objective` で明示する**。既定は「動画の見どころ候補の抽出」。用途に応じて上書きする。

## 全体の流れ

```text
Step 1 全体把握    動画全体を低fps(0.5)でタイル化 → overview解析 → 候補を多めに列挙
        │
Step 2 候補確認    候補をconfig化 → 一括タイル化(中fps) → detail解析 → 事実を確定
        │
Step 2.5 ズーム    文字・細部が読めない候補だけ特定時刻をフル解像度で確認（任意）
        │
Step 3 精密確認    境界が曖昧な候補だけ高fpsで再タイル化 → refine解析（任意）
        │
Step 4 最終出力    確定した見どころ候補だけをまとめる
```

各ステップは **「① タイル化 → ② 解析 → ③ 判断」** の繰り返し。使うスクリプトは2本だけ。


| 役割                                         | スクリプト                              |
| ------------------------------------------ | ---------------------------------- |
| タイル化（単一範囲 / config複数範囲 / zoom）             | `scripts/tile_video_frames.py`     |
| 解析（単一 / 複数manifest / summary一括 / 並列 / 分割） | `scripts/analyze_tile_manifest.py` |


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
  zooms/         # ズーム（特定時刻フル解像度）のタイルと解析結果
  refinements/   # 精密確認タイルと解析結果
  notes/         # 候補一覧などの中間メモ
  final.md       # 最終まとめ
```

中間結果はテキストだけで保持せず、各範囲の解析出力を必ずファイルに残す。

## 出力形式（JSON）

3プロンプト（overview/detail/refine）と zoom はコードフェンス付きJSONを返す。解析は `--expect-json` を付けて実行し、JSONを検証・整形保存する。

- 成功時: `<name>_analysis.json`（整形済み）と `<name>_analysis.raw.txt`（生出力）。
- 失敗時: 1回だけリトライし、なお失敗なら生テキストを残して継続（バッチは止めない）。終了時に失敗件数を報告する。

各プロンプトは `{{OBJECTIVE}}` を含み、`--objective` で目的を差し込める。タイル画像の読み方（セルの読み順・絶対時刻など）はスクリプト側が自動で付加するため、プロンプトには書かない。

---

## Step 1: 全体把握

**目的**: 動画の概要を掴み、見どころになりそうな範囲を多めに列挙する。

### ① タイル化（全体を fps=0.5）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --fps 0.5 --output output/agentic_sessions/example/overview/full.jpg
```

- `--start` / `--end` 省略で動画全体。フレームが多ければ自動で複数タイル（`tile_000.jpg`…）に分割され、`manifest.json` が書かれる。
- まず **fps=0.5** を試す。候補列挙が目的ならこれで十分なことが多い。

**長尺動画（10分超）の階層化**:

- **≤ 10分**: 上記どおり fps=0.5 で一括。
- **> 10分**: まず **fps=0.1〜0.2** で「章立て把握」を行い、大きな区切り（章）を掴む。次に章ごとに fps=0.5 の overview を実施する（章を範囲として config モードで一括処理すると速い）。全編を一律高fpsにしない。

### ② 解析（overviewプロンプト）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --manifest output/agentic_sessions/example/overview/full/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt --objective "動画の見どころ候補の抽出" --expect-json --output output/agentic_sessions/example/overview/full_analysis.txt
```

- manifest内の全タイルが**原則1回のAPI呼び出し**でまとめて渡される。
- タイル数が `--max-tiles-per-call`（既定8）を超えると、時系列順に**1タイル重複**で分割して複数回呼び出し、`_part00.json`… を保存後に機械統合した `_analysis.json` を生成する。
- **分割時は同一候補がパート境界で重複しうる**。統合JSONの `candidates` に重複が出たら、時刻が近く内容が同じものは1件に統合して扱う（判断はエージェント側）。

### ③ 判断

overview JSON の `candidates` から `notes/candidates.md` を機械的に作る。各候補の `priority` / `needs_followup` をそのまま引き継ぐ。

```markdown
- candidate_a (38.0-46.0s): priority=high, needs_followup=yes, reason=大きな画面変化
- intro (0.0-12.0s): priority=low, needs_followup=no, reason=導入部
```

→ `priority=high` または `needs_followup=true` の候補を Step 2 へ。`reason` は次段の `note`（仮説）に流用できる。

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
    { "label": "candidate_a", "start": 38, "end": 46, "priority": "high", "note": "38s付近で大きな画面変化。何が起きたか確認したい" },
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
- range ごとにタイル＋`manifest.json` を出力し、全rangeの `manifest_path` と `note` を含む `batch_summary.json` を書く。
- range に **`note`**（全体把握での観察＝仮説）を付けると、次の detail 解析でプロンプトへ注入される。

### ② 解析（detailプロンプト・summaryで一括）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --summary output/agentic_sessions/example/candidates/batch_summary.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt --expect-json --jobs 4
```

- `--summary` で全rangeのmanifestを自動列挙し、**range ごとに1回ずつ**解析する。
- `--jobs N` で複数rangeを並列解析（既定1）。出力先が衝突する場合は事前にエラーになる。
- 各rangeの `note` は「事前の仮説」として注入され、モデルは `hypothesis_verdict`（confirmed/partially/rejected/n/a）で確認・反証する。
- 出力は省略時 manifest 横に `<range名>_analysis.json` で自動命名（`--output-dir` でまとめ先を指定可）。

### ③ 判断

detail JSON をもとに `notes/candidates.md` を更新する。次の分岐に使う:

- `hypothesis_verdict` が `rejected` → その候補は見送り寄り（採否判断は最終段だが、根拠として記録）。
- `confidence: low` または境界がタイル端 → **Step 3（精密確認）**へ。
- `zoom_targets`（文字・UIが読めない時刻）がある、または細部起因で `confidence: low` → **Step 2.5（ズーム）**へ。

さらに見たい箇所が出たら候補を足してよい（1反復で追加は最大3件程度）。

---

## Step 2.5: ズーム確認（任意）

**目的**: 文字・UI・小さなオブジェクトなど、タイルの縮小では読めない細部を、特定時刻の**フル解像度**フレームで判読する。

detail の `zoom_targets` や、細部起因で `confidence: low` の候補に対して行う。

### ① タイル化（特定時刻をズーム）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --timestamps 39.4,41.0 --output output/agentic_sessions/example/zooms/cand_a.jpg
```

- `--timestamps` は `--start/--end/--fps` と排他。各時刻を1枚=1画像でフル解像度（既定リサイズなし・品質90）抽出する。
- config の range に `"timestamps": [39.4, 41.0]` を書けば、detail の `zoom_targets` を一括ズーム化できる。

### ② 解析（zoomプロンプト）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --manifest output/agentic_sessions/example/zooms/cand_a/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/zoom.txt --expect-json --output output/agentic_sessions/example/zooms/cand_a_analysis.txt
```

- `readable_text` に判読できた文字列、`unreadable` に読めなかった要素が返る。**読めないものは推測で補完しない**。

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
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --manifest output/agentic_sessions/example/refinements/range_38_46_fps9/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/refine.txt --expect-json --output output/agentic_sessions/example/refinements/range_38_46_fps9_analysis.txt
```

- `start_sec` / `end_sec` を最良推定、`suggested_pad_before_sec` / `suggested_pad_after_sec` で余白の目安が返る。

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

### fps戦略

| 段階              | fps            | 目的            |
| --------------- | -------------- | ------------- |
| 長尺の章立て（10分超）    | 0.1〜0.2        | 大きな区切りの把握     |
| Step 1 全体把握     | 0.5〜1          | 概要と候補列挙       |
| Step 2 候補確認     | 3〜5            | 出来事の存在確認      |
| Step 2.5 ズーム    | 特定時刻のみ・フル解像度   | 文字・細部の判読      |
| Step 3 精密確認     | 8〜10           | 開始・終了秒の詰め     |


### タイル設計

- 1タイルあたり **12〜16枚** を目安（`--frames-per-tile`、既定12）。超えると自動分割される。
- 1回のAPI呼び出しは **タイル8枚まで**（`--max-tiles-per-call`、既定8）。超過は1タイル重複で分割・統合。
- 候補・精密確認では前後 **2〜3秒** のパディング（`--pad`）を付け、境界の出来事を取りこぼさない。
- 精密確認の1範囲は **おおよそ10秒以内**。

### 再タイル化する / しない


| する                                         | しない                       |
| ------------------------------------------ | ------------------------- |
| `priority=high` / `needs_followup=true` の候補 | `priority=low` で追加確認不要な区間 |
| 画面構成・状態が大きく変わる箇所                           | 変化の乏しい導入部・待機部・説明部         |
| 出来事の開始・終了がタイル端にかかる                         | すでに十分な根拠が取れている候補          |
| モデルが `confidence: low` と判断した箇所             | —                         |


- 1回の反復で追加する候補は **最大3件** 程度。
- 既存の詳細範囲と **50%以上重なる** 候補は、新規追加せず既存範囲へマージ（`--merge-overlaps`）。

---

## スクリプトリファレンス

### tile_video_frames.py

各セルに `F<index> t=<秒>s` のラベルが付く。`t=<秒>s` は動画内の絶対時刻（600秒超は `m:ss.s` 表記）。

**単一範囲モード**（`--video` 必須）:

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --start 38 --end 46 --pad 2 --fps 5 --output output/agentic_sessions/example/candidates/range_38_46_fps5.jpg
```

**zoomモード**（`--timestamps` 指定時。`--start/--end/--fps` と排他）:

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --timestamps 39.4,41.0 --output output/agentic_sessions/example/zooms/cand_a.jpg
```

**configモード**（`--config` 指定時は `--start/--end` 等を無視）:

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --config path/to/ranges.json --merge-overlaps
```


| オプション                                      | 説明                                          |
| ------------------------------------------ | ------------------------------------------- |
| `--video` / `-v`                           | 入力動画パス（単一範囲・zoomモードで必須）                     |
| `--config` / `-c`                          | 範囲定義JSON。複数範囲を一括処理                          |
| `--start` / `--end`                        | 開始秒 / 終了秒。省略時は0秒 / 動画末尾                     |
| `--pad`                                    | 開始・終了の前後に足す秒数                               |
| `--fps`                                    | 1秒あたりの抽出枚数                                  |
| `--timestamps`                             | zoom: 抽出時刻をカンマ区切りで指定（フル解像度で1枚ずつ）           |
| `--frames-per-tile` / `-f`                 | 1タイルあたりの最大フレーム数（推奨12〜16）                    |
| `--width`                                  | ffmpeg抽出時のリサイズ幅px（既定640。zoomは未指定でフル解像度）    |
| `--tile-width` / `--tile-height`           | タイル画像の目標サイズ（既定1600×900）                     |
| `--quality`                                | 出力JPEG品質（タイル既定80 / zoom既定90）                |
| `--output` / `-o`                          | 出力パス。複数タイル時は同名ディレクトリに `tile_000.jpg` 等を出力   |
| `--metadata-output`                        | manifest JSON の出力先。省略時は出力先の `manifest.json` |
| `--merge-overlaps` / `--overlap-threshold` | configモードで重なる範囲をマージ（既定しきい値0.5）              |
| `--dry-run`                                | configモードで処理予定の範囲を表示するだけ                    |


configのrangeキー（`label`, `start`, `end`, `fps`, `pad`, `frames_per_tile`, `width`, `note`, `timestamps` …）は対応する単一範囲オプションと同名。`defaults` に共通値、各 range で上書き。`note` は仮説として detail 解析に注入。`timestamps` を持つ range は zoom として処理される。

### analyze_tile_manifest.py

manifest内の全タイルを **原則1回のAPI呼び出し** で `aitool recognize-image` に渡す。複数manifestはmanifestごとに1回ずつ呼ぶ（「1範囲=1呼び出し」を維持）。モデル既定は `google/gemini-3.5-flash`。

```bash
# 単一
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --manifest path/to/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt --expect-json --output path/to/analysis.txt

# 複数（タイル化summaryから自動列挙・並列）
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py --summary path/to/batch_summary.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt --expect-json --jobs 4
```


| オプション                    | 説明                                                        |
| ----------------------- | --------------------------------------------------------- |
| `--manifest` / `-m`     | manifest.json のパス（複数指定可）                                  |
| `--summary` / `-s`      | `batch_summary.json`。`results[].manifest_path` を自動列挙      |
| `--prompt` / `-p`       | プロンプトテキストファイル（バッチ内は共通）                                    |
| `--objective`           | 解析の目的（テキスト or ファイルパス）。プロンプトの `{{OBJECTIVE}}` を置換          |
| `--context`             | 追加コンテキスト（テキスト or ファイルパス）。プロンプト末尾に付加                      |
| `--expect-json`         | 出力をJSON検証し `.json` に整形保存。失敗時1回リトライ、なお失敗なら生テキスト保存で継続       |
| `--jobs`                | 複数manifestの並列ワーカー数（既定1）                                   |
| `--max-tiles-per-call`  | 1呼び出しのタイル上限（既定8）。超過は1タイル重複で分割・統合                          |
| `--output` / `-o`       | 出力パス（manifestが1件のときのみ）。省略時はmanifest横に自動命名                 |
| `--output-dir`          | 複数manifestの出力をまとめるディレクトリ                                  |
| `--model`               | 使用モデル（既定 `google/gemini-3.5-flash`）                       |
| `--dry-run`             | 実行コマンドを表示するだけ                                             |


### プロンプトテンプレート

- 全体把握: [prompts/overview.txt](prompts/overview.txt)
- 候補確認: [prompts/detail.txt](prompts/detail.txt)
- ズーム: [prompts/zoom.txt](prompts/zoom.txt)
- 精密確認: [prompts/refine.txt](prompts/refine.txt)

いずれも `{{OBJECTIVE}}` とコードフェンス付きJSON出力に対応。境界が曖昧な候補だけ、必要に応じて別モデルで再確認してもよいが、通常は `google/gemini-3.5-flash` だけでよい。

---

## 並列化

複数候補のタイル化・解析など独立した処理は並列実行してよい。解析は `--jobs N` でmanifest単位に並列化できる。`tile_video_frames.py --config` は複数範囲を逐次処理するため、さらに速度が必要なら config を分割して並列に起動する。低密度で十分な区間まで高fpsで全編を再タイル化しない。

## 中間成果物

各ステップで最低限これを残す。

- タイル画像と `manifest.json`
- 範囲ごとの `*_analysis.json`（+ `*_analysis.raw.txt`）
- configモードの `batch_summary.json`
- `notes/candidates.md`（候補一覧メモ）
