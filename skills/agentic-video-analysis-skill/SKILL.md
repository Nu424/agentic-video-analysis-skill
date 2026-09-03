---
name: agentic-video-analysis-skill
description: |
  OpenRouter または Gemini API で、動画を「全体把握 → 全区間カバーの範囲計画 → 高密度の詳細解析 → 検証 → 統合」の
  2段階で反復解析し、見どころ候補を網羅的に抽出するスキル。
  動画解析、見どころ抽出、ハイライト候補探索、タイムライン化、実況・プレイ映像の分析、
  エージェンティックな動画理解を行うときに使用する。
---

# Agentic Video Analysis

動画を一度に全部見て判断するのではなく、**低密度で全体を把握してから、全区間を短い範囲に割って高密度で見直す**。
この2段階の反復構造が精度を決める。1コマンドの標準経路（`run_pipeline.py`）で回し、その結果を見て追加反復を判断する。

## 原則

- **2段階が本質**。全体把握（低fps）→ 範囲ごとの詳細解析（高fps）。全編を一度に高fpsで見ても、全編を一度に低fpsで見ても網羅率は伸びない。先行検証で、この反復構造の有無が入力形式やモデル世代よりはるかに大きな差を生むことを確認している。
- **全区間をカバーする**。候補の付いた範囲だけを見ない。候補で覆われない隙間は `gap_NN` として低優先で埋め、末尾の範囲は必ず動画の終端に届かせる。取りこぼしは末尾と範囲の境界に偏る。
- **偽陰性 < 偽陽性**。全体把握では候補を多めに挙げる。疑わしい出来事は消さず、フラグを付けて残す。
- **映像上の根拠だけで書く**。セルラベル `t=<秒>s`（動画内の絶対時刻）を根拠に含める。見えていないものを補完しない。読めないものは `unreadable` に返す。
- **既定値は実測に基づく**。fps・モデル・範囲長の既定は先行検証で決めたもの。変えるときは `eval/` で測ってから変える（1回の結果で判断しない）。
- **このスキルのゴールは「見どころ候補の抽出」**。採用/不採用の最終判断はしない。判断材料（根拠・確信度・フラグ）を揃えて渡す。

---

## セットアップ

| 項目 | 用途 | 必須か |
| --- | --- | --- |
| `ffmpeg` / `ffprobe`（PATH上） | フレーム抽出・動画長取得 | tile 経路で必須。native 経路でも `ffprobe` は使う |
| Python 3.11+ と `Pillow` | タイル画像の生成 | tile 経路で必須 |
| `OPENROUTER_API_KEY` | 既定バックエンド | `--backend openrouter` で必須 |
| `GEMINI_API_KEY` と `google-genai` | ネイティブ動画クリップ・音声 | `--backend gemini` で必須 |

**バックエンドとキーの対応**

| `--backend` | 必要なキー | 追加パッケージ | 既定モデル | できること |
| --- | --- | --- | --- | --- |
| `openrouter`（既定） | `OPENROUTER_API_KEY` | 不要（標準ライブラリのみ） | `google/gemini-3.7-flash` | 画像（タイル）。最安・監査しやすい |
| `gemini` | `GEMINI_API_KEY` | `google-genai` | `gemini-3.7-flash` | 画像 + ネイティブ動画クリップ + 音声 |

キーは 環境変数 → カレントの `.env` → `~/.env.global` の順に探す（`KEY="value"` のクォートは剥がされる）。

```bash
python -m pip install -r .agents/skills/agentic-video-analysis-skill/scripts/requirements.txt
```

ローカルに入れたくない場合は `uv` で一時実行してよい（以降の `python ...` を `uv run --with Pillow python ...` に読み替える）。

```bash
uv run --with Pillow python .agents/skills/agentic-video-analysis-skill/scripts/run_pipeline.py --help
```

**入力形式の選び方**

| 組み合わせ | 特徴 | 使いどころ |
| --- | --- | --- |
| `--backend openrouter --input tile`（既定） | 最安。タイル画像とセルラベルが残るので人間が根拠を確認できる | 迷ったらこれ |
| `--backend gemini --input tile` | 同上 | OpenRouter を使わない場合 |
| `--backend gemini --input native` | ばらつきが小さい。音声も扱える。画像は残らない | ffmpeg が使えない環境、音声も見たい場合 |

`--input native` は `--backend gemini` のときだけ使える。網羅率の差は測定のばらつきの範囲で、精度で選ぶ必要はない。

---

## 標準ワークフロー（1コマンド）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/run_pipeline.py --video video.mp4 --objective "動画の見どころ候補の抽出" --domain notes/domain.json
```

`output/agentic_sessions/<動画名>_<日時>/` に成果物が揃う。**各ステップは主要な出力が既にあればスキップ**するので、
途中で止まっても同じコマンドで再開できる（全部やり直すなら `--force`）。まず `--dry-run` を付けて範囲数と呼び出し予定を見てから本番実行してよい
（dry-run の仮計画は本番実行で作り直されるので、**同じセッションをそのまま使える**）。

| Step | 処理 | 主な出力 | 既定 |
| --- | --- | --- | --- |
| 0 | セッション作成、`ffprobe` で長さ取得、`domain.json` をコピー | `session.json`, `notes/domain.json` | |
| 1 | **全体把握**: 全編を低fpsでタイル化して解析。候補は全区間をカバーするよう短い単位で出る | `overview/full/`, `overview/overview_analysis.json` | overview fps 1 |
| 1' | 長尺（既定 600 秒超）は先に章立て → 章ごとに overview | `overview/chapters_analysis.json`, `overview/<章>_analysis.json` | chapters fps 0.15 |
| 2 | **範囲計画**: 候補をマージ・分割し、隙間を low で埋め、末尾を必ず含む計画を作る | `ranges/ranges.json` | 範囲長 ≤ 8 秒、pad 1.0 |
| 3 | tile 経路のみ: 全範囲を一括タイル化 | `ranges/<label>_fps<fps>/`, `ranges/batch_summary.json` | detail fps 5、12枚/タイル |
| 4 | **詳細解析**: 範囲ごとに出来事を悉皆列挙。overview の `reason` が仮説として注入され `hypothesis_verdict` で確認・反証される | `ranges/<label>_fps<fps>_analysis.json`（+ `.raw.txt` / `.meta.json` / `.prompt.txt`） | 並列 4、範囲ごとに失敗を隔離 |
| 5 | **バリデーション**: ネガティブ一致・根拠なし・範囲外引用・境界・長さ異常・低確信にフラグを付ける（削除はしない） | `ranges/<label>_fps<fps>_validated.json`, `merge/validation_report.json` | |
| 6 | **統合**: 機械的に重複統合 → LLM統合（追加禁止）→ `final.md` | `merge/timeline_mechanical.json`, `merge/timeline.json`, `final.md` | `--no-llm-merge` で機械統合のみ |
| 7 | **レポート**: 呼び出し回数・トークン・USD・所要時間と「次にやること」 | 標準出力（`usage.jsonl` 集計） | |

主なオプション: `--backend` / `--input` / `--model` / `--objective` / `--domain` / `--coverage` / `--detail-fps` /
`--jobs` / `--session`（既存セッションを指定すると再開）/ `--session-root`（セッションを作る親ディレクトリ）/
`--full-coverage-max-sec`（長尺分岐に入る秒数）/ `--no-llm-merge` / `--force` / `--dry-run`。

---

## 終了時の判断

ドライバは終了時に「次にやること」を出す。**それを読んで、必要な追加反復だけを行う。** 実際の出力はこの形。

```text
## 次にやること
1. バリデーションのフラグが 10 件あります。<session>/merge/validation_report.json を開き、negative_match は根拠セルを画像で目視、boundary は範囲を広げて再解析してください
2. zoom_targets が 4 件あります（0.2s, 6.7s, 14.2s, 21.7s）。tile_video_frames.py --timestamps <秒> でズームし、prompts/zoom.txt で確認してください
3. confidence=low の出来事が 4 件あります。細部が原因ならズーム（prompts/zoom.txt）、範囲の境界が原因なら精密確認（prompts/refine.txt）を行ってください
```

対応の指針:

1. **失敗した範囲** → 同じコマンドをもう一度実行する（完了済みはスキップされ、失敗分だけ走る）。
2. **`negative_match`** → タイル画像の根拠セルを目視、またはズーム確認。確認できなければ final.md の「注意点」に「未確認」と書く。**消さない**。
3. **`low_confidence` / `zoom_targets`** → 細部起因ならズーム、境界起因なら精密確認。
4. **`boundary`** → 精密確認、または範囲を広げて再解析。
5. **`hypothesis_rejected`** → 全体把握の誤認。final.md の「注意点」に残す（そのまま捨てない）。
6. **コスト** → 想定より大きければ coverage を落として再計画する。

追加反復は「1反復で追加する範囲は3件程度まで」を目安にし、全編を高fpsで再解析しない。

---

## 追加反復

### ズーム確認（文字・UI・小さな物体）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --timestamps 39.4,41.0 --output <session>/zooms/cand_a.jpg
python .agents/skills/agentic-video-analysis-skill/scripts/analyze.py --manifest <session>/zooms/cand_a/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/zoom.txt --session <session>
```

`zoom_targets` の時刻をそのまま使う。config の range に `"timestamps": [...]` を書けば一括化できる。読めないものは `unreadable` に返る。

### クロップズーム（状態表示の数値など）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --timestamps 34.0,34.4 --crop 280:200:0:0 --scale 2 --output <session>/zooms/hud_34.jpg
```

`--crop W:H:X:Y` は ffmpeg の crop 記法、`--scale` は整数倍拡大。切り出す領域は `domain.json` の `hud_notes` に書かれていればそれに従い、
無ければ先にフルフレームのズームで位置を確かめる。**スクリプトは画面の意味を知らない**ので、領域はエージェントが決める。

### 精密確認（開始・終了秒を詰める）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --start 48 --end 54 --pad 1 --fps 9 --output <session>/refinements/range_48_54_fps9.jpg
python .agents/skills/agentic-video-analysis-skill/scripts/analyze.py --manifest <session>/refinements/range_48_54_fps9/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/refine.txt --session <session>
```

1範囲は10秒以内。**fps 8〜10 はここだけ**で使う（全編や detail で上げても精度は上がらず誤認が増える）。
native 経路なら動画クリップを直接送れる。

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/analyze.py --video video.mp4 --start 48 --end 54 --fps 10 --backend gemini --prompt .agents/skills/agentic-video-analysis-skill/prompts/refine.txt --output <session>/refinements/range_48_54_native_analysis.json
```

### 範囲の追加・再解析

overview に無かった箇所を見たいとき、境界を広げたいとき。`examples/ranges.example.json` の形式で追加範囲を書く（`note` に仮説を書く）。

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --config <session>/ranges/extra.json --merge-overlaps
python .agents/skills/agentic-video-analysis-skill/scripts/analyze.py --summary <session>/ranges/extra_summary.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt --session <session> --jobs 4
python .agents/skills/agentic-video-analysis-skill/scripts/validate_analysis.py --summary <session>/ranges/extra_summary.json --domain <session>/notes/domain.json
python .agents/skills/agentic-video-analysis-skill/scripts/merge_analyses.py --session <session> --final-md
```

既存範囲と50%以上重なるなら新規に作らず既存範囲を広げる（`--merge-overlaps`）。
`merge_analyses.py --session` はセッション配下の `*_validated.json`（無ければ `*_analysis.json`）を集め直すので、追加分も自動で入る。

### 再実行と和集合（網羅率を上げる）

「見逃しがありそう」と指摘されたときの標準手順。**既定は1回しか回さない。**

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/run_pipeline.py --video video.mp4 --objective "動画の見どころ候補の抽出" --session output/agentic_sessions/<stem>_run2
python .agents/skills/agentic-video-analysis-skill/scripts/merge_analyses.py --union output/agentic_sessions/<stem>_run1/merge/timeline.json output/agentic_sessions/<stem>_run2/merge/timeline.json --output output/agentic_sessions/<stem>_run1/merge/timeline_union.json --final-md
```

見逃しはランダムに散るので、同じ設定でもう1回回して和集合を取るのが最も安く効く（先行検証で確認済み）。
各項目に `runs` が付き、片方だけが拾った項目が分かる。3回目以降は効果が逓減する。箇所が特定できているなら和集合より範囲追加のほうが安い。

### 音声（gemini バックエンドのみ）

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/analyze_audio.py --video video.mp4 --session <session> --backend gemini
python .agents/skills/agentic-video-analysis-skill/scripts/merge_analyses.py --session <session> --audio <session>/audio/audio_analysis.json --final-md
```

必ず全編を送る（範囲を絞ると映像を見ずに音声だけから内容を捏造する）。音声由来の項目は `source: "audio"` で timeline に入り、
映像側の出来事と重ならないものには `audio_unconfirmed` が付く。**音声の主張は映像根拠と突き合わせてから採用する。**

### モデル・バックエンドを変える

`--model` / `--backend` を変えて別セッションで回し、`eval/` で比べる。**新しいモデルが良いとは限らない**
（隣接世代の差は測定のばらつきに埋もれ、コストと誤認だけ増えた実測がある）。既定は測って決めたもの。

---

## 手動ステップ実行（ドライバを使わない場合）

overview を人が書いた範囲で置き換えたい、fps を段階ごとに変えたい等。ドライバは以下を順に呼んでいるだけなので、成果物の形は同じ。

```bash
# Step 1 全体把握
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --fps 1 --output <session>/overview/full.jpg
python .agents/skills/agentic-video-analysis-skill/scripts/analyze.py --manifest <session>/overview/full/manifest.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt --objective "動画の見どころ候補の抽出" --domain <session>/notes/domain.json --output <session>/overview/overview_analysis.json --session <session>

# Step 2 範囲計画（全区間カバー）
python .agents/skills/agentic-video-analysis-skill/scripts/plan_ranges.py --overview <session>/overview/overview_analysis.json --video video.mp4 --output <session>/ranges/ranges.json --coverage full --detail-fps 5

# Step 3 タイル化（tile 経路のみ）
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --config <session>/ranges/ranges.json --merge-overlaps

# Step 4 詳細解析（tile 経路）
python .agents/skills/agentic-video-analysis-skill/scripts/analyze.py --summary <session>/ranges/batch_summary.json --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt --objective "動画の見どころ候補の抽出" --domain <session>/notes/domain.json --session <session> --jobs 4

# Step 4' 詳細解析（native 経路。--backend gemini 限定。--output-dir は必須）
python .agents/skills/agentic-video-analysis-skill/scripts/analyze.py --ranges <session>/ranges/ranges.json --backend gemini --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt --output-dir <session>/ranges --session <session> --jobs 4

# Step 5 バリデーション
python .agents/skills/agentic-video-analysis-skill/scripts/validate_analysis.py --summary <session>/ranges/batch_summary.json --domain <session>/notes/domain.json --report

# Step 6 統合と final.md
python .agents/skills/agentic-video-analysis-skill/scripts/merge_analyses.py --session <session> --objective "動画の見どころ候補の抽出" --domain <session>/notes/domain.json --final-md

# Step 7 レポート
python .agents/skills/agentic-video-analysis-skill/scripts/session_report.py --session <session>
```

各ステップ後の判断基準は「終了時の判断」と同じ。

---

## 長尺動画

動画長が `--full-coverage-max-sec`（既定 600 秒）を超えると、ドライバは自動でこの経路に入る。全編を一律に高fpsで解析しない。

1. **章立て**: 全編を `--chapters-fps`（既定 0.15）でタイル化し、`prompts/chapters.txt` で大きな区切りを列挙する。
2. **章ごとの overview**: 章を範囲とする config を作って一括タイル化（fps 1）し、`prompts/overview.txt` で章ごとに候補を出す。
3. **範囲計画**: `--coverage priority`（長尺の既定）。high/medium は detail fps、low は `--low-fps`（既定 1）。コストが厳しければ `high-only`。
4. 以降は標準と同じ。

解析前の概算は `session_report.py --session <session> --estimate` で出る（`ranges/ranges.json` があれば範囲数とタイル数からコストを見積もる）。
超過しそうなら coverage を落として計画し直す。

---

## 目的とドメイン定義

- **目的**（`--objective`）: 何を抽出するか。テキストでもファイルパスでもよい。プロンプト内の `{{OBJECTIVE}}` に差し込まれる。
  既定は「動画の見どころ候補の抽出」。例:「操作手順の区切りの列挙」「特定の表示が変化した時刻の特定」。
- **ドメイン定義**（`--domain domain.json`、任意）: **スキル本体は特定ドメインを知らない**。精度を上げたいときはここに書く。
  ユーザーが動画のジャンルや見てほしい観点を伝えてきたら、エージェントはまず簡単な `domain.json` を作ってから解析に入る（無くても動く）。
  ドライバに渡すと `notes/domain.json` にコピーされ、全ステップで使われる。

雛形は [examples/domain.example.json](examples/domain.example.json)。キーの役割:

| キー | 使われ方 |
| --- | --- |
| `name` / `description` | ドメインの説明。プロンプトの「ドメインの手引き」に入る |
| `hud_notes` | 画面上の状態表示の位置と読み方。クロップズームの領域を決めるときにも読む |
| `watchlist` | 注視すべき変化の一覧。detail の見落としを減らす |
| `vocabulary` | 用語 → 映像上でどう見えるか。**映像的な特徴で書く**（名前だけ並べない） |
| `negatives` | 誤認されやすい事象。`pattern`（正規表現）と任意の `window` をバリデータが使い、`name` はプロンプトにも入る |
| `importance_rubric` | LLM統合で重要度を付ける基準 |

誤認は「もっともらしい固有名詞の補完」として出る（プロンプトの言い回しだけでは消えない）。`negatives` に書いて後段で検出する。

自由テキストの `--context` も併用できる（プロンプト末尾に付く）。

---

## 最終出力 final.md

`merge_analyses.py --final-md` が `timeline.json` から機械生成する。構成は固定。

```markdown
## 概要
[動画全体の要約]

## 見どころ候補
### 1. [タイトル] ([start_sec]s - [end_sec]s)
- **title**:
- **start_sec** / **end_sec**:
- **summary**:
- **根拠**: [根拠テキスト / 出所（範囲ラベル）]
- **補足**: importance=… / confidence=… / flags=… / runs=…

## 追加確認した範囲
- [範囲ラベル]: [start]s-[end]s（[件数]件）

## タイムライン要約
- [start]s-[end]s [importance] [タイトル]

## 注意点
- [フラグの付いた項目、反証された仮説、音声未確認など]
```

- 「見どころ候補」は `importance` が low 以外の項目。「タイムライン要約」は全項目。
- **根拠には解析ファイル名とセルラベル**（例: `F12 t=39.4s`）が入る。tile 経路ならそのセルを人間が画像で確認できる。
- 「注意点」はエージェントが追記してよい（未確認のフラグ、反証された仮説、音声を見ていないこと等）。生の中間出力を流し込まない。

---

## パラメータ早見表

### fps 戦略

| 段階 | fps | 目的 |
| --- | --- | --- |
| 長尺の章立て | 0.1〜0.2（既定 0.15） | 大きな区切りの把握 |
| 全体把握 | 1（既定） | 概要と候補列挙 |
| 詳細解析 | 5（既定） | 出来事の悉皆列挙 |
| 低優先範囲（priority モード） | 1（既定） | 隙間の最低限の確認 |
| ズーム | 特定時刻のみ・フル解像度 | 文字・細部の判読 |
| 精密確認 | 8〜10 | 開始・終了秒の詰め |

### 既定値と根拠

| 項目 | 既定 | 根拠 |
| --- | --- | --- |
| モデル | `google/gemini-3.7-flash` | 先行検証でこの世代が網羅率とコストの両方で最良。**隣接世代に上げても差はばらつきに埋もれ、誤認だけ増えた** |
| detail fps | 5 | fps を上げてもコストと誤認が増えるだけで網羅率は伸びなかった |
| 範囲長 | ≤ 8 秒（最小 2 秒） | 長い範囲は出来事を落とす。短すぎると文脈が失われる |
| pad | 1.0 | 全区間カバーでは隣接範囲が重なるため、v1 の 2 秒から下げた |
| frames_per_tile | 12 | 超えると自動でタイル分割 |
| max-tiles-per-call | 8 | 超過分は1タイル重複で分割し、結果を機械統合 |
| jobs | 4 | 範囲ごとに失敗が隔離されるので並列で問題ない |
| coverage | ≤ 600 秒は `full`、超過は `priority` | 全区間カバーのコストは動画長に比例する |

**変えないほうがよいもの**: detail fps 5、既定モデル。どちらも上げると誤認とコストが増えるのに網羅率が伸びないことを測って確認している。
変えるときは `eval/` で同一動画・複数回で測る。

---

## スクリプトリファレンス

すべての CLI は UTF-8 で出力する。LLM を呼ぶものは `--dry-run` で実行予定だけを表示できる。

### run_pipeline.py — 標準ドライバ

`--video`（必須） / `--objective` / `--domain` / `--backend {openrouter,gemini}` / `--input {tile,native}` /
`--session` / `--session-root` / `--model` / `--overview-fps` / `--detail-fps` / `--low-fps` / `--chapters-fps` /
`--coverage {auto,full,priority,high-only}` / `--max-range-sec` / `--pad` / `--frames-per-tile` / `--jobs` /
`--full-coverage-max-sec` / `--no-llm-merge` / `--strict-json` / `--force` / `--dry-run`

### tile_video_frames.py — タイル化

各セルに `F<index> t=<秒>s` のラベルが付く（600秒超は `m:ss.s` 表記）。モードは3つ。

```bash
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --start 38 --end 46 --pad 1 --fps 5 --output <session>/ranges/range_38_46.jpg
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --video video.mp4 --timestamps 39.4,41.0 --crop 280:200:0:0 --scale 2 --output <session>/zooms/cand_a.jpg
python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py --config <session>/ranges/ranges.json --merge-overlaps
```

| オプション | 説明 |
| --- | --- |
| `--video` / `-v` | 入力動画パス（単一範囲・zoom モードで必須） |
| `--config` / `-c` | 範囲定義JSON。複数範囲を一括処理（`--start/--end` 等は無視される） |
| `--start` / `--end` / `--pad` / `--fps` | 単一範囲モードの指定 |
| `--timestamps` | zoom: 抽出時刻をカンマ区切り（`--start/--end/--fps` と排他）。フル解像度で1枚ずつ |
| `--crop` / `--scale` | zoom: `W:H:X:Y` の切り出しと整数倍拡大 |
| `--frames-per-tile` / `-f` | 1タイルあたりの最大フレーム数（既定12） |
| `--width` | ffmpeg 抽出時のリサイズ幅px（既定640。zoom は未指定でフル解像度） |
| `--tile-width` / `--tile-height` / `--cols` / `--cell-width` / `--gap` / `--label-height` | タイルの見た目 |
| `--quality` | 出力JPEG品質（タイル既定80 / zoom既定90） |
| `--output` / `-o` | 出力パス。複数タイルになる場合は同名ディレクトリに `tile_000.jpg` 等 |
| `--metadata-output` | manifest の出力先（省略時は出力先の `manifest.json`） |
| `--merge-overlaps` / `--overlap-threshold` | config モードで重なる範囲をマージ（既定しきい値 0.5） |
| `--dry-run` | config モードで処理予定の範囲を表示するだけ |

config の range キー（`label` / `start` / `end` / `fps` / `pad` / `frames_per_tile` / `width` / `crop` / `scale` /
`note` / `priority` / `timestamps` …）は単一範囲オプションと同名。`defaults` に共通値を書き、range 側で上書きする。
`note` は仮説として detail 解析に注入される。`timestamps` を持つ range は zoom として処理される。雛形: [examples/ranges.example.json](examples/ranges.example.json)

### plan_ranges.py — 全区間カバーの範囲計画

`--overview`（overview 解析JSON） / `--video` / `--output`（必須）。
`--coverage {auto,full,priority,high-only}` / `--detail-fps` / `--low-fps` / `--max-range-sec` / `--min-range-sec` /
`--min-gap-sec` / `--pad` / `--frames-per-tile` / `--output-dir` / `--full-coverage-max-sec`

候補をマージ・分割し、`--min-gap-sec` 以上の隙間を `gap_NN`（priority=low）で埋め、先頭を 0 秒、末尾を動画終端に揃える。

### analyze.py — 解析

入力は4通り: `--manifest`（複数可） / `--summary`（`batch_summary.json` から自動列挙） /
`--ranges`（範囲ごとにネイティブ動画クリップ、`--backend gemini` 限定、`--output-dir` 必須） /
`--video --start --end --fps`（単一範囲をネイティブクリップで、gemini 限定）。

| オプション | 説明 |
| --- | --- |
| `--prompt` / `-p` | プロンプトファイル（必須） |
| `--objective` / `--context` / `--domain` | 目的・追加コンテキスト・ドメイン定義 |
| `--backend` / `--model` / `--api-key` | バックエンド指定（`--api-key` はプロセス一覧に出るので非推奨） |
| `--raw` | JSON検証をしない（**既定はJSON検証 + 失敗時1回リトライ**） |
| `--strict-json` | スキーマがある呼び出しで構造化出力を要求する |
| `--session` | `usage.jsonl` に記録を追記（省略時は出力先から上方向に `session.json` を探す） |
| `--force` / `--strict` | 既存出力も再解析 / 1件でも失敗したら終了コード1 |
| `--max-tiles-per-call` | 1呼び出しのタイル上限（既定8）。超過は1タイル重複で分割・統合 |
| `--jobs` | 複数対象の並列ワーカー数（既定1） |
| `--output` / `-o` / `--output-dir` | 出力先。省略時は manifest 横に `<name>_analysis.json` |
| `--dry-run` | backend / model / メディア / プロンプト先頭 / 出力先を表示するだけ |

出力は `<name>_analysis.json`（整形済み）、`<name>_analysis.raw.txt`（生出力）、`<name>_analysis.meta.json`（usage・コスト・リトライ回数）、`<name>_analysis.prompt.txt`（送ったプロンプト）。

### validate_analysis.py — 後段バリデーション

`--summary` または `--analysis`（複数可）で対象を指定。`--domain` / `--max-event-sec`（既定30） /
`--output-dir` / `--report` / `--report-output` / `--session`。
**出来事は削除しない。** 各 event に `flags` と `confidence_adjusted` を足した `<name>_validated.json` を書く。

| フラグ | 意味 |
| --- | --- |
| `negative_match` | `domain.json` の `negatives[].pattern` に一致（誤認の疑い） |
| `no_cell_evidence` | セルラベル（`F\d+` / `t=`）の引用が無い |
| `evidence_out_of_range` | 引用された時刻が範囲（pad込み）の外 |
| `boundary` | 範囲端に接している。精密確認か範囲拡張の候補 |
| `duration_outlier` | `end < start`、または長すぎる |
| `low_confidence` | 元の confidence が low |
| `hypothesis_rejected` | 全体把握の仮説が反証された |

### merge_analyses.py — 統合・和集合・final.md

`--session`（配下の `*_validated.json` を集める。overview / zooms / refinements / audio は対象外）または `--inputs`。
`--union`（複数実行の timeline を合成、各項目に `runs`）/ `--audio` / `--output` / `--final-md` / `--output-md` /
`--objective` / `--domain` / `--no-llm`（LLM統合を省く）/ `--llm-chunk`（既定80）/ `--prompt` /
`--backend` / `--model` / `--api-key` / `--strict-json` /
`--overlap-threshold`（既定0.5）/ `--title-similarity`（既定0.6）/ `--report-output`。

機械統合の結果（`timeline_mechanical.json`）は必ず残る。LLM統合前後の件数差分は `validation_report.json` に記録される。

### analyze_audio.py — 音声解析（gemini 限定）

`--video`（必須） / `--session` / `--output-dir` / `--backend`（既定 gemini） / `--model` / `--api-key` /
`--objective` / `--prompt` / `--strict-json` / `--force` / `--dry-run`。ドメイン定義を渡す `--domain` も受け付ける。
必ず全編をまとめて送る（範囲を絞ると捏造する）。出力 `audio/audio_analysis.json`。

### session_report.py — コスト集計と次アクション

`--session`（必須） / `--estimate`（解析前の概算コスト） / `--json`。

### プロンプト

| ファイル | 用途 |
| --- | --- |
| [prompts/chapters.txt](prompts/chapters.txt) | 長尺の章立て |
| [prompts/overview.txt](prompts/overview.txt) | 全体把握（全区間をカバーする候補列挙） |
| [prompts/detail.txt](prompts/detail.txt) | 範囲ごとの悉皆列挙 |
| [prompts/zoom.txt](prompts/zoom.txt) | 文字・細部の判読 |
| [prompts/refine.txt](prompts/refine.txt) | 開始・終了秒の詰め |
| [prompts/merge.txt](prompts/merge.txt) | 統合（**追加禁止**、重複排除のみ） |
| [prompts/audio.txt](prompts/audio.txt) | 音声（聞こえたことだけ） |

いずれも `{{OBJECTIVE}}` に対応し、コードフェンス付きJSONを返す。
タイル画像の読み方（セルの読み順・絶対時刻）はスクリプトが自動で付けるので、プロンプトには書かない。

---

## 成果物一覧

```text
output/agentic_sessions/<video_stem>_<timestamp>/
  session.json                      動画・長さ・backend・model・objective・domain・各ステップの実行記録
  usage.jsonl                       LLM呼び出しごとの usage / cost / latency（1行1呼び出し）
  overview/
    full/tile_*.jpg, manifest.json  全体把握のタイル（tile 経路）
    overview_analysis.json          候補範囲（+ .raw.txt / .meta.json / .prompt.txt）
    chapters_analysis.json          長尺のみ。章立ての結果
  ranges/
    ranges.json                     全区間カバーの範囲計画（config 形式）
    batch_summary.json              タイル化結果と各範囲の note / error
    <label>_fps<fps>/               範囲ごとのタイルと manifest.json（例: cand_00_0_fps5.0/）
    <label>_fps<fps>_analysis.json  detail の出来事リスト
    <label>_fps<fps>_validated.json flags 付き
  zooms/                            ズーム・クロップの画像と解析（任意）
  refinements/                      精密確認の画像と解析（任意）
  audio/audio_analysis.json         音声（gemini のみ、任意）
  merge/
    timeline_mechanical.json        機械統合の結果（LLM統合前の監査用）
    timeline.json                   最終タイムライン
    timeline_union.json             和集合（任意）
    validation_report.json          フラグ集計、LLM統合前後の件数差分
  final.md                          最終まとめ
  notes/domain.json                 使ったドメイン定義のコピー
```

中間結果をテキストだけで保持せず、必ずファイルに残す。

---

## 判断早見表

| 状況 | 行動 |
| --- | --- |
| 標準実行が終わった | 「次にやること」を読む。失敗範囲 → 同じコマンドを再実行。フラグ → 該当する追加反復 |
| `negative_match` が付いた | 根拠セルを目視 or ズーム。確認できなければ「未確認」として残す。**消さない** |
| `low_confidence`、細部起因 | ズーム。数値・アイコンならクロップズーム |
| `low_confidence` / `boundary`、境界起因 | 精密確認。範囲端なら範囲を広げて再解析 |
| 「見逃しがある」と言われた | 箇所が分かるなら範囲追加。分からなければ再実行して和集合 |
| 「この出来事は本当か」と聞かれた | tile 経路なら画像パスとセルラベルを提示。native なら fps を上げて再解析 |
| 音声情報が要る | gemini バックエンドで `analyze_audio.py`。映像根拠と突き合わせる |
| 動画が長い | 長尺経路（自動）。coverage を落とし、`--estimate` で事前見積もりを出す |
| コストが想定超 | coverage を落とす。detail fps は上げない。和集合は2回まで |
| プロンプトや既定値を変えたい | `eval/` で測ってから。1回の結果で判断しない |

---

## 注意（既知の制約）

- **同じセッションを複数プロセスから同時に更新しない**。`session.json` と `usage.jsonl` は追記型で排他制御をしていない。並列が要るなら `--jobs` を使う（範囲ごとの並列は安全）か、セッションを分ける。
- **Gemini の Files API にアップロードした動画は約48時間で失効する**。セッション内キャッシュは ACTIVE を確認してから再利用するので、失効していれば再実行時に自動で再アップロードされる。
- **コストは概算のことがある**。`usage.cost` を返さないモデルでは単価表から計算し、レポートに「概算」と出る。
- JSON検証に失敗し続けるときは `.raw.txt` を見る。出力が長すぎるなら範囲を短くする。`--strict-json` で構造化出力を強制する手もある。
- `--input native` は `--backend gemini` 限定（OpenRouter は動画クリップ非対応）。矛盾する指定はエラーになる。
- Windows で日本語が化けるときは `PYTHONUTF8=1`（CLI 側は UTF-8 出力に固定済み）。
