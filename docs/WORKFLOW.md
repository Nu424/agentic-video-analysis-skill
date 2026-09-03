# 分析ワークフロー（改修後）

作成日: 2026-09-03
対応する作業書: [WORKPLAN-v2.md](WORKPLAN-v2.md)

この文書は、改修後のスキルで**エージェントが実際にどう動くか**を網羅的に説明する。
SKILL.md（P7 で改訂）はこの文書を圧縮したものになる。コマンドのパスは
`skills/agentic-video-analysis-skill/scripts/` を `S` と略記する。

---

## 0. 前提とセットアップ

### 0.1 必要なもの

| 項目 | 用途 | 必須か |
|------|------|-------|
| `ffmpeg` / `ffprobe` | フレーム抽出・動画長取得 | tile 経路で必須。native 経路でも `ffprobe` は使う |
| Python 3.11+ と `Pillow` | タイル描画 | tile 経路で必須 |
| `OPENROUTER_API_KEY` | 既定バックエンド（OpenRouter 直接呼び出し） | `--backend openrouter` で必須 |
| `GEMINI_API_KEY` と `google-genai` | Gemini バックエンド（ネイティブ動画クリップ・音声） | `--backend gemini` で必須 |

キーは環境変数 → カレントの `.env` → `~/.env.global` の順で探す。`aitool` は不要になった。

### 0.2 バックエンドと入力形式の選び方

| 組み合わせ | 網羅率（実測） | コスト | 音声 | 監査 | 使いどころ |
|-----------|--------------|-------|------|------|-----------|
| `--backend openrouter --input tile`（既定） | 92.6% | 最安（約 $0.12 / 1 分動画） | 不可 | タイル画像とセルラベルが残る | 迷ったらこれ。根拠を人間が確認したい用途 |
| `--backend gemini --input tile` | 同上 | 同程度 | 不可 | 同上 | OpenRouter を使いたくない場合 |
| `--backend gemini --input native` | 94.1%（ばらつき最小） | 約 $0.16 | 可 | 画像は残らない | ffmpeg を使えない環境、音声も見たい用途 |

`--input native` は `--backend gemini` のときだけ使える。網羅率の差はばらつきの範囲内で、精度で選ぶ必要はない。

### 0.3 目的とドメインの用意

- **目的**（`--objective`）: 何を抽出するか。例:「動画の見どころ候補の抽出」「操作手順の区切りの列挙」「特定の表示が変化した時刻の特定」。既定は見どころ候補の抽出。
- **ドメイン定義**（`--domain domain.json`、任意）: 画面上の状態表示の位置と読み方、注視すべき変化、誤認されやすい事象の正規表現。
  雛形は `examples/domain.example.json`。**スキル本体は特定ドメインを知らない**ので、精度を上げたいときはここに書く。
  ユーザーが動画のジャンルを伝えてきたら、エージェントはまず簡単な `domain.json` をセッション内に作ってから解析に入る（無くても動く）。

---

## 1. 標準ワークフロー（1 コマンド）

```bash
python S/run_pipeline.py --video video.mp4 --objective "動画の見どころ候補の抽出" --domain notes/domain.json
```

これで以下が順に走り、`output/agentic_sessions/<stem>_<timestamp>/` に成果物が揃う（親ディレクトリは `--session-root` で変えられる）。
各ステップは主要な出力が既にあればスキップされるので、途中で止まっても同じコマンドで再開できる（全部やり直すなら `--force`）。

先に `--dry-run` を付けると、範囲数・fps・呼び出し予定だけを表示して API を呼ばない（タイル化は実行する）。
dry-run で作られるのは仮の計画なので、**同じセッションのまま `--dry-run` を外して本番実行してよい**（計画は作り直される）。

### 1.1 内部で起きること

| Step | 処理 | 主な出力 | 既定値 |
|------|------|---------|-------|
| 0 | セッション作成、`ffprobe` で長さ取得 | `session.json` | |
| 1 | **全体把握**: 全編を低 fps でタイル化し `overview.txt` で解析。候補は全区間をカバーするよう 4〜8 秒単位で出る | `overview/full/`, `overview/overview_analysis.json` | overview fps 1（native は動画全体を fps 1 で送る） |
| 1' | 動画が `--full-coverage-max-sec`（既定 600 秒）超なら先に章立て（`chapters.txt`, `--chapters-fps` 既定 0.15）→ 章ごとに overview | `overview/chapters_analysis.json`, `overview/<章>_analysis.json` | |
| 2 | **範囲計画**: 候補をマージ・分割し、隙間を low で埋め、末尾を必ず含む `ranges.json` を作る | `ranges/ranges.json` | 範囲長 ≤ 8 秒、pad 1.0、coverage は 600 秒以下なら `full` |
| 3 | tile 経路のみ: 全範囲を一括タイル化 | `ranges/<label>_fps<fps>/tile_*.jpg`, `manifest.json`, `ranges/batch_summary.json` | detail fps 5、12 枚/タイル |
| 4 | **詳細解析**: 範囲ごとに `detail.txt` で出来事を悉皆列挙。overview の `reason` が仮説として注入され、`hypothesis_verdict` で確認・反証される | `ranges/<label>_fps<fps>_analysis.json` (+ `.raw.txt`, `.meta.json`, `.prompt.txt`) | 並列 4、範囲ごとに失敗隔離 |
| 5 | **バリデーション**: ネガティブ一致・根拠引用なし・範囲外引用・境界・長さ異常・低確信にフラグ | `ranges/<label>_fps<fps>_validated.json`, `merge/validation_report.json` | 削除はしない |
| 6 | **統合**: 機械的に重複を統合 → `merge.txt` で LLM 統合（追加禁止）→ `final.md` 生成 | `merge/timeline_mechanical.json`, `merge/timeline.json`, `final.md` | `--no-llm-merge` で機械統合のみ |
| 7 | **レポート**: 呼び出し回数・トークン・USD・所要時間と**次アクション候補**を表示 | `usage.jsonl` 集計 | |

### 1.1' ドライバの主なオプション

| オプション | 既定 | 用途 |
|-----------|------|------|
| `--objective` / `--domain` | 見どころ候補の抽出 / なし | 目的とドメイン定義（§0.3）。`--domain` は `notes/domain.json` にコピーされ全ステップで使われる |
| `--backend` / `--input` / `--model` | openrouter / tile / backend 既定 | バックエンドと入力形式（§0.2） |
| `--session` / `--session-root` | 新規作成 / `output/agentic_sessions` | 既存セッションの再開 / セッションを作る親ディレクトリ |
| `--overview-fps` / `--detail-fps` / `--low-fps` / `--chapters-fps` | 1 / 5 / 1 / 0.15 | 各段階の fps |
| `--coverage` | `auto` | `full` / `priority` / `high-only`（§4） |
| `--full-coverage-max-sec` | 600 | この秒数を超えたら長尺分岐（章立て + coverage priority）に入る |
| `--max-range-sec` / `--pad` / `--frames-per-tile` / `--jobs` | 8 / 1 / 12 / 4 | 範囲計画とタイル化・並列度 |
| `--no-llm-merge` | off | Step 6 で LLM 統合を省き、機械統合の結果をそのまま `timeline.json` にする |
| `--strict-json` / `--force` / `--dry-run` | off | 構造化出力の強制 / 全ステップ再実行 / 実行予定の表示のみ |

### 1.2 終了時にエージェントが確認すること

`session_report.py` の出力（ドライバ終了時にも同じものが出る）と、ドライバが最後に出す「次にやること」を読み、次を判断する。

```text
## 次にやること
1. バリデーションのフラグが 10 件あります。<session>/merge/validation_report.json を開き、negative_match は根拠セルを画像で目視、boundary は範囲を広げて再解析してください
2. zoom_targets が 4 件あります（0.2s, 6.7s, 14.2s, 21.7s）。tile_video_frames.py --timestamps <秒> でズームし、prompts/zoom.txt で確認してください
3. confidence=low の出来事が 4 件あります。細部が原因ならズーム（prompts/zoom.txt）、範囲の境界が原因なら精密確認（prompts/refine.txt）を行ってください
```

1. **失敗した範囲**があるか → `analyze.py --summary ranges/batch_summary.json --prompt prompts/detail.txt` を再実行（既存出力はスキップされるので失敗分だけ走る）。
2. **`negative_match`** のフラグ → その範囲のタイル画像（tile 経路）を開いて根拠セルを目視、またはズーム確認（§2.1）。確認できなければ final.md の「注意点」に「未確認」と書く。採用/不採用の判断はしない。
3. **`low_confidence` / `zoom_targets`** → 細部起因ならズーム（§2.1）、境界起因なら精密確認（§2.3）。
4. **`boundary`** → 範囲端にかかる出来事。精密確認（§2.3）か、範囲を広げて再解析（§2.4）。
5. **`hypothesis_rejected`** → overview の仮説が反証された。final.md の「注意点」に記録する（overview の誤認候補としての価値がある）。
6. **コスト**が想定より大きいか。長尺なら coverage を `priority` / `high-only` に落として再計画する（§4）。

追加反復は「1 反復で追加する範囲は 3 件程度まで」を目安にし、全編を高 fps で再解析しない。

---

## 2. エージェンティックな追加反復

標準ワークフローの結果を見て、必要なものだけ行う。全てセッションディレクトリの中に積み上げる。

### 2.1 ズーム確認（文字・UI・小さな物体）

```bash
python S/tile_video_frames.py --video video.mp4 --timestamps 39.4,41.0 --output <session>/zooms/cand_a.jpg
python S/analyze.py --manifest <session>/zooms/cand_a/manifest.json --prompt prompts/zoom.txt --session <session>
```

- `zoom_targets` の時刻をそのまま使う。config の range に `"timestamps": [...]` を書けば一括化できる。
- 読めなかったものは `unreadable` に返る。**推測で埋めない**。

### 2.2 ROI クロップズーム（状態表示の数値など）

```bash
python S/tile_video_frames.py --video video.mp4 --timestamps 34.0,34.4 --crop 280:200:0:0 --scale 2 --output <session>/zooms/hud_34.jpg
```

- `domain.json` の `hud_notes` に「どこを切り出すと読めるか」が書いてあればそれに従う。無ければフルフレームのズーム（§2.1）で位置を確認してから切り出す。
- 数値の増減や小さなアイコンの同定は、タイルの縮小セルでは潰れる。ここが `resolution: high` 相当の役割を果たす。

### 2.3 精密確認（開始・終了秒を詰める）

```bash
python S/tile_video_frames.py --video video.mp4 --start 48 --end 54 --pad 1 --fps 9 --output <session>/refinements/range_48_54_fps9.jpg
python S/analyze.py --manifest <session>/refinements/range_48_54_fps9/manifest.json --prompt prompts/refine.txt --session <session>
```

- 1 範囲は 10 秒以内。fps 8〜10 は**ここだけ**で使う（全編や detail で上げても精度は上がらず誤認が増える）。
- native 経路なら `analyze.py --video video.mp4 --start 48 --end 54 --fps 10 --backend gemini --prompt prompts/refine.txt`。0.2 秒精度が出る。
- 結果の `start_sec / end_sec` で `timeline.json` の該当項目を更新し、`merge_analyses.py --final-md` で final.md を作り直す。

### 2.4 範囲の追加・再解析

overview に無かった箇所を見たくなったとき、または境界を広げたいとき。

```bash
# ranges/extra.json に追加範囲を書く（examples/ranges.example.json の形式。note に仮説を書く）
python S/tile_video_frames.py --config <session>/ranges/extra.json --merge-overlaps
python S/analyze.py --summary <session>/ranges/extra_summary.json --prompt prompts/detail.txt --session <session> --jobs 4
python S/validate_analysis.py --summary <session>/ranges/extra_summary.json --domain notes/domain.json
python S/merge_analyses.py --session <session> --final-md     # 全範囲を拾い直して統合
```

- 既存範囲と 50% 以上重なるなら新規に作らず既存範囲を広げる（`--merge-overlaps`）。
- `merge_analyses.py --session` はセッション内の全 `_validated.json`（無ければ `_analysis.json`）を集めるので、追加分も自動で入る。

### 2.5 仮説の反証を活かす

detail の `hypothesis_verdict` が `rejected` の範囲は、overview が誤認した箇所。final.md の「注意点」に「全体把握では X と見えたが詳細解析で否定された」と残す。
人間が正解データを作る場面でも「モデルが違うと言い出したら、まず正解データを疑う」が実証されているので、反証は捨てない。

### 2.6 再実行と和集合（網羅率を上げる）

人間から「見逃しがありそう」「精度が足りない」と指摘されたときの標準手順。**既定では 1 回しか回さない。**

```bash
python S/run_pipeline.py --video video.mp4 --objective "…" --domain notes/domain.json --session output/agentic_sessions/<stem>_run2
python S/merge_analyses.py --union output/agentic_sessions/<stem>_run1/merge/timeline.json output/agentic_sessions/<stem>_run2/merge/timeline.json --output output/agentic_sessions/<stem>_run1/merge/timeline_union.json --final-md
```

- 同じ設定でもう 1 回回すのが最も安い（実測 92.6% → 95%）。入力形式を変えても効果はほぼ同じ。
- 和集合は機械的（時間重なりと題名の類似で同一視）。各項目に `runs` が付くので、片方だけが拾った項目が分かる。
- 3 回目以降は効果が逓減する。指摘された箇所が特定できるなら、和集合より §2.4 の範囲追加の方が安い。

### 2.7 音声（gemini バックエンドのみ）

```bash
python S/analyze_audio.py --video video.mp4 --session <session> --backend gemini
python S/merge_analyses.py --session <session> --audio <session>/audio/audio_analysis.json --final-md
```

- 必ず全編を送る（範囲を絞ると映像を見ずに音声から内容を捏造した実測がある）。
- `--objective` と `--domain` を渡せば、目的とドメインの手引きが音声プロンプトにも入る。出力は `<session>/audio/audio_analysis.json`（既にあればスキップ。`--force` で再解析）。
- 音声由来の項目は `source: "audio"` で timeline に入り、映像側の出来事と重ならないものは `audio_unconfirmed` フラグが付く。**音声の主張は映像根拠と突き合わせてから採用する。**

### 2.8 モデル・バックエンドを変えて再実行する

`--model` / `--backend` を変えて別セッションで回し、§5 の評価で比べる。**新しいモデルが良いとは限らない**（隣接世代は差が sd に埋もれ、コストと誤認だけ増えた実測がある）。既定は測って決めた `google/gemini-3.7-flash`。

---

## 3. 手動ステップ実行（ドライバを使わない場合）

ドライバが合わないとき（overview を人が書いた範囲で置き換えたい、fps を段階ごとに変えたい等）は、同じステップを個別に実行する。
ドライバは以下を順に呼んでいるだけなので、成果物の形は同じ。

```bash
# Step 1 全体把握
python S/tile_video_frames.py --video video.mp4 --fps 1 --output <session>/overview/full.jpg
python S/analyze.py --manifest <session>/overview/full/manifest.json --prompt prompts/overview.txt --objective "…" --domain notes/domain.json --output <session>/overview/overview_analysis.json --session <session>

# Step 2 範囲計画（全区間カバー）
python S/plan_ranges.py --overview <session>/overview/overview_analysis.json --video video.mp4 --output <session>/ranges/ranges.json --coverage full --detail-fps 5

# Step 3 タイル化（tile 経路のみ）
python S/tile_video_frames.py --config <session>/ranges/ranges.json --merge-overlaps

# Step 4 詳細解析
python S/analyze.py --summary <session>/ranges/batch_summary.json --prompt prompts/detail.txt --objective "…" --domain notes/domain.json --session <session> --jobs 4
#   native 経路（--backend gemini 限定。--output-dir は必須）:
#   python S/analyze.py --ranges <session>/ranges/ranges.json --backend gemini --prompt prompts/detail.txt --output-dir <session>/ranges --session <session>

# Step 5 バリデーション
python S/validate_analysis.py --summary <session>/ranges/batch_summary.json --domain notes/domain.json --report

# Step 6 統合と final.md
python S/merge_analyses.py --session <session> --objective "…" --domain notes/domain.json --final-md

# Step 7 レポート
python S/session_report.py --session <session>
```

各ステップ後の判断基準は §1.2 と同じ。

---

## 4. 長尺動画（既定 600 秒超）

1. **章立て**: `tile_video_frames.py --video long.mp4 --fps 0.15` → `analyze.py --prompt prompts/chapters.txt`。大きな区切り（場面転換・画面構成の変化）を列挙する。fps は `--chapters-fps`（既定 0.15）。
2. **章ごとの overview**: 章を範囲とする config を作り `--config` で一括タイル化（fps 1）→ `analyze.py --summary … --prompt prompts/overview.txt`。章ごとの結果はドライバが 1 つの `overview_analysis.json` に統合する。
3. **範囲計画**: `plan_ranges.py --coverage priority`（長尺の既定）。high/medium は `--detail-fps`（5）、low は `--low-fps`（1）。コストが厳しければ `high-only`。
4. 以降は標準と同じ。`session_report.py --session <session> --estimate` で解析前に範囲数 × fps からトークンとコストの概算が出るので、超過するなら coverage を落とす。

ドライバは動画長が `--full-coverage-max-sec`（既定 600）秒を超えると自動的にこの分岐に入る。全編を一律に高 fps で解析しない。

---

## 5. 評価ワークフロー（精度を測る）

出力を鵜呑みにしない。プロンプト・既定値・モデルを変えるときは必ず測る。詳細は `eval/README.md`。

1. **正解データを人手で作る**: 全編を fps 3 でタイル化し（`tile_video_frames.py --fps 3 --frames-per-tile 28`）、自分の目で見る。争点は fps 8〜10 で見直す。出来事を原子的な単位で書き、カテゴリを付け、**存在しないことを確認した事象**（ネガティブコントロール）も列挙する。
2. **照合ルールを埋め込む**: `match.window`（前後 0.5〜1 秒の余裕）と `match.any`（言い換えを広く）。`negatives[].pattern` は正規表現。
3. **採点**: `python eval/score.py <session>/merge/timeline.json --gt eval/fixtures/<name>/ground_truth.json`。網羅率・カテゴリ別・誤認数・コスト。
4. **比較**: 変数は 1 つだけ変え、各条件 3 回以上（差が小さそうなら 5 回）。平均と sd を並べる。`eval/union_recall.py` で和集合の効果も見る。
5. **正解データも疑う**: モデル出力と食い違ったら、まず映像で正解データを確認する（人手の正解が 2 件間違っていた実測がある）。訂正は `revision_note` に残す。

---

## 6. 成果物一覧

```text
output/agentic_sessions/<stem>_<timestamp>/
  session.json                     動画・長さ・backend・model・objective・domain・実行記録
  usage.jsonl                      LLM 呼び出しごとの usage / cost / latency（1 行 1 呼び出し）
  overview/
    full/tile_*.jpg, manifest.json overview のタイル（tile 経路）
    overview_analysis.json         候補範囲（+ .raw.txt / .meta.json / .prompt.txt）
    chapters_analysis.json         長尺のみ。章立ての結果
    chapters_ranges.json           長尺のみ。章を範囲とする config
    chapters_summary.json          長尺のみ。章ごとのタイル化結果
    <章ラベル>_analysis.json        長尺のみ。章ごとの overview
  ranges/
    ranges.json                    全区間カバーの範囲計画（config 形式）
    batch_summary.json             タイル化結果と各範囲の note / error
    <label>_fps<fps>/tile_*.jpg, manifest.json   例: cand_00_0_fps5.0/
    <label>_fps<fps>_analysis.json detail の出来事リスト
    <label>_fps<fps>_validated.json flags 付き
  zooms/                           ズーム・クロップの画像と解析（任意）
  refinements/                     精密確認の画像と解析（任意）
  audio/audio_analysis.json        音声（gemini のみ、任意）
  merge/
    timeline_mechanical.json       機械統合の結果（LLM 統合前の監査用）
    timeline.json                  最終タイムライン
    timeline_union.json            和集合（任意）
    validation_report.json         フラグ集計、LLM 統合前後の件数差分
  final.md                         最終まとめ（timeline から生成。注意点はエージェントが追記）
  notes/                           domain.json のコピー、候補メモなど
```

**final.md の構成**は v1 と同じ（概要 / 見どころ候補 / 追加確認した範囲 / タイムライン要約 / 注意点）で、
`merge_analyses.py --final-md` が `timeline.json` から機械生成する。「見どころ候補」は `importance` が low 以外の項目、
「タイムライン要約」は全項目、「注意点」はフラグの付いた項目と反証された仮説。
各候補の「根拠」には、解析ファイル名とセルラベル（`ranges/cand_00_0_fps5.0_analysis.json`, `F12 t=39.4s`）を必ず書く。tile 経路ならそのセルを人間が画像で確認できる。

---

## 7. 判断基準の早見表

| 状況 | 行動 |
|------|------|
| ドライバの標準実行が終わった | `session_report` を読む。失敗範囲 → 再実行。フラグ → §2 の該当項目 |
| `negative_match` が付いた | タイルの根拠セルを目視 or ズーム。確認できなければ「未確認」として残す。消さない |
| `low_confidence`、細部起因 | ズーム（§2.1）。数値・アイコンならクロップ（§2.2） |
| `low_confidence` / `boundary`、境界起因 | 精密確認（§2.3）。範囲端なら範囲を広げて再解析（§2.4） |
| 人間から「見逃しがある」 | 箇所が分かるなら範囲追加（§2.4）。分からなければ再実行と和集合（§2.6） |
| 人間から「この出来事は本当か」 | 根拠セルを提示。tile 経路なら画像パスとセルラベル、native なら再解析（fps 10 / high）で確認 |
| 音声情報が要る | gemini バックエンドで §2.7。映像根拠と突き合わせる |
| 動画が 10 分超 | §4。coverage を `priority` に。事前見積もりを出す |
| コストが想定超 | coverage を落とす。detail fps は 5 から上げない。和集合は 2 回まで |
| プロンプトや既定値を変えたい | §5 で測ってから。1 回の結果で判断しない |

---

## 8. トラブルシューティング

| 症状 | 原因と対処 |
|------|-----------|
| `OPENROUTER_API_KEY` / `GEMINI_API_KEY` が無いと言われる | バックエンドとキーの対応（§0.1）。`~/.env.global` の値がクォート付きでも読める |
| `--input native` でエラー | native は `--backend gemini` 限定。openrouter は動画クリップ非対応 |
| JSON 検証失敗が続く | `.raw.txt` を見る。長すぎる出力なら範囲を短くする（範囲長 ≤ 8 秒）。`--strict-json` で構造化出力を強制してみる |
| 一部の範囲だけ失敗 | `batch_summary.json` の `error` を見て同じコマンドを再実行（既存出力はスキップされる） |
| Windows で日本語が化ける | CLI は UTF-8 出力に固定済み。それでも化けるなら `PYTHONUTF8=1` |
| Gemini のアップロードが失効した | 約 48 時間で失効。セッション内キャッシュは ACTIVE 確認をするので、そのまま再実行すれば再アップロードされる |
| コストが読めない | `usage.jsonl` の `cost_usd` が無いモデルは単価表で概算。`session_report` に「概算」と出る |
