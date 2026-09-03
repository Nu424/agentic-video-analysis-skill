# 改修作業書 v2: agentic-video-analysis-skill

作成日: 2026-09-03
前提ブランチ: `feature/workplan-improvements`（v1 作業書 P0〜P4 完了済み）
関連: [docs/WORKFLOW.md](WORKFLOW.md)（改修後の分析ワークフロー）、[docs/WORKPLAN.md](WORKPLAN.md)（v1 作業書）

この文書は、本セッションの統率者（Claude）が立案し、実装はサブエージェントが担当する。
**読み手は実装担当の AI**。各フェーズは、この文書と参照ファイルだけで着手できるように書く。

---

## 1. 背景: 検証で分かったこと

別リポジトリで、53.6 秒の動画に人手で 27 事象の正解データを作り、
手法・モデル・入力形式を比較した（[レポート](../../try-gemini-agenticvideo/docs/gemini-agentic-video-comparison.md)）。
本改修はその結論を汎用スキルに取り込むもの。要点は 6 つ。

| # | 事実 | 本改修での扱い |
|---|------|--------------|
| 1 | 網羅率を決めるのは**入力形式ではなく反復構造**。全編一括 33〜44% → 区間分割+高密度再解析 93〜100% | 構造は維持。Step 2 を「候補だけ」から**全区間カバー**に変える（§4.3） |
| 2 | `gemini-3.5-flash` → `3.7-flash` で +11〜16pt、コスト 1/3〜1/4。`3.7 → 3.8` は差なし・誤認増 | 既定モデルを 3.7 に（§4.2）。差し替えは測ってから、を評価基盤で担保（§4.10） |
| 3 | detail fps は 5 で十分。fps 10 はコスト +46%、誤認 1.7 倍、精度据え置き | 既定 5。fps 8〜10 は refine 限定 |
| 4 | 見逃しはランダム。**同条件 2 回の和集合で 92.6 → 95%** | 和集合をオプションとして整備（既定は 1 回）（§4.6） |
| 5 | 誤認は固有名詞のドラマ補完（モデル世代を跨いで残る）。プロンプトだけでは消えない | 後段バリデータ + ドメイン定義ファイルのネガティブリスト（§4.4, §4.5） |
| 6 | 落とす場所が偏る: **動画末尾**、**タイル/範囲の境界**。1 範囲の API 失敗で全体が落ちた事例あり | 末尾必須範囲・pad 重複・境界フラグ、範囲ごとの失敗隔離とレジューム（§4.3, §4.7） |

副次的な知見: タイル方式はネイティブより 21% 安く、タイル画像が残るので**監査可能**。ネイティブは音声を扱え、ばらつきが小さい。
両方使えるようにし、用途で選ぶ。

---

## 2. 設計方針

1. **汎用性**: 特定ドメインの知識をプロンプト・コードに入れない。ドメイン知識は `domain.json`（§4.4）で外から注入する。
   プロンプトの語彙は「画面上の状態表示」「視覚エフェクト」「テキスト表示」のような抽象語で書く。
2. **エージェンティック + 標準ドライバ**: 定型経路（全体把握 → 範囲計画 → 詳細解析 → 検証 → 統合）は 1 コマンド（`run_pipeline.py`）で走らせ、
   その結果を見てエージェントが追加反復（ズーム・精密確認・範囲追加・再実行と和集合）を判断する。
   プリミティブ CLI は残し、ドライバはそれらを呼ぶだけの薄い層にする。
3. **バックエンド抽象**: `aitool` 依存を外し、OpenRouter を直接叩く。Gemini API（ネイティブ動画クリッピング + 音声）も選べる。
   解析ロジックはバックエンドを知らない。
4. **監査可能性**: 全ての LLM 呼び出しについて、送ったプロンプト・メディア一覧・usage・コスト・生出力をファイルに残す。
5. **既定値は実測に基づく**: モデル 3.7 / overview fps 1 / detail fps 5 / pad 1.0 / 範囲長 4〜8 秒。変えるときは `eval/` で測る。
6. **落ちない・再開できる**: 範囲単位で失敗を隔離し、出力が既にあるものはスキップする。
7. **スクリプトは薄く、ロジックはパッケージへ**: 1,600 行の 2 CLI を `avs/` パッケージに分割し、CLI は引数解釈とパッケージ呼び出しだけにする。

---

## 3. 改修後の構成

```text
agentic-video-analysis-skill/            # リポジトリ
  README.md
  docs/
    WORKPLAN.md                          # v1（履歴）
    WORKPLAN-v2.md                       # 本書
    WORKFLOW.md                          # 改修後の分析ワークフロー（SKILL.md の元）
  eval/                                  # 評価基盤（スキル本体には含めない。§4.10）
    README.md                            # 正解データの作り方・採点手順（汎用化）
    score.py                             # 正解データ照合・網羅率・誤認数・コスト
    union_recall.py                      # 複数実行の和集合網羅率
    ground_truth.example.json            # 正解データの雛形（抽象的な例）
    fixtures/                            # 実データ置き場（git 管理外）
  tests/                                 # pytest。合成動画で API 不要の検証
  skills/agentic-video-analysis-skill/   # npx skills add で配布される単位
    SKILL.md
    prompts/
      overview.txt   detail.txt   refine.txt   zoom.txt
      merge.txt      audio.txt    chapters.txt          # 新規 3 本
    examples/
      ranges.example.json
      domain.example.json                # ドメイン定義の雛形（抽象的なプレースホルダ）
    scripts/
      requirements.txt                   # Pillow（google-genai は gemini バックエンド利用時のみ）
      run_pipeline.py                    # 【新規】標準経路を 1 コマンドで
      tile_video_frames.py               # 薄い CLI（+ --crop / --scale）
      plan_ranges.py                     # 【新規】overview 結果 → 全区間カバーの ranges.json
      analyze.py                         # 【改名】analyze_tile_manifest.py → analyze.py（manifest / summary / ranges 入力）
      validate_analysis.py               # 【新規】後段バリデーション
      merge_analyses.py                  # 【新規】統合・和集合・final.md 生成
      analyze_audio.py                   # 【新規】音声解析（gemini バックエンド限定）
      session_report.py                  # 【新規】コスト集計と次アクション候補
      avs/                               # 【新規】パッケージ（agentic video skill）
        __init__.py
        common.py                        # ffprobe / JSON IO / タイムスタンプ / フォント / UTF-8 出力
        session.py                       # セッションディレクトリ、session.json、usage.jsonl
        tiling.py                        # フレーム抽出・タイル描画・manifest（旧 tile_video_frames の本体）
        ranges.py                        # config 読み込み・defaults 合成・重複マージ・全区間カバー計画
        prompts.py                       # プロンプト組み立て（objective / tile context / 仮説 / domain / context）
        analysis.py                      # 1 manifest or 1 範囲の解析: チャンク分割・JSON 検証・リトライ・meta 保存
        validate.py                      # バリデーションルール
        merge.py                         # 機械的重複統合・和集合・LLM 統合・final.md
        cost.py                          # usage 正規化・単価表・集計
        backends/
          __init__.py                    # get_backend(name, model, api_key)
          base.py                        # LLMRequest / LLMResponse / Backend Protocol
          openrouter.py                  # 直接 HTTP（標準ライブラリ urllib）
          gemini.py                      # google-genai Interactions API（画像 + ネイティブ動画クリップ + 音声）
```

`analyze_tile_manifest.py` は削除する（互換シムは置かない。v1 の呼び出し元は本スキル内だけ）。

---

## 4. 主要な設計

### 4.1 パッケージ化（挙動不変のリファクタ）

- `tile_video_frames.py`（1,015 行）の関数群を `avs/tiling.py`（抽出・描画・manifest・zoom）と `avs/ranges.py`（config・マージ）に移す。CLI には `parse_args` と `main` だけ残す。
- `analyze_tile_manifest.py` の `build_tile_context / apply_objective / build_hypothesis_block / assemble_prompt` は `avs/prompts.py`、
  `chunk_with_overlap / merge_part_results / extract_json / analyze_one` は `avs/analysis.py` へ。
- manifest v2 形状、config 形式、出力ファイル命名（`<name>_analysis.json` / `.raw.txt` / `_partNN`）は維持する。
- 全 CLI 冒頭で `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` を行う（Windows cp932 で日本語出力が落ちる問題の恒久対策）。
- `from avs import ...` は `scripts/` を `sys.path[0]` とする通常起動で動く。`python -m` 不要。

### 4.2 バックエンド抽象

`avs/backends/base.py`:

```python
@dataclass
class MediaImage:
    path: Path                       # JPEG/PNG。base64 で送る

@dataclass
class MediaVideoClip:                # gemini バックエンドのみ
    video: Path
    start_sec: float
    end_sec: float
    fps: float
    resolution: str = "high"         # "low" | "high"

@dataclass
class LLMRequest:
    prompt: str
    media: list[MediaImage | MediaVideoClip]
    json_schema: dict | None = None  # 構造化出力を要求する場合
    model: str | None = None         # None なら backend の既定

@dataclass
class LLMResponse:
    text: str
    usage: dict                      # 正規化キー: input_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd(任意), raw
    latency_sec: float
    model: str
    backend: str

class Backend(Protocol):
    name: str
    default_model: str
    supports_video_clip: bool
    supports_audio: bool
    def complete(self, request: LLMRequest) -> LLMResponse: ...
```

**openrouter.py**（既定バックエンド）

- `POST https://openrouter.ai/api/v1/chat/completions`。標準ライブラリ `urllib.request` のみ使う（新規依存を増やさない）。
- `messages=[{"role":"user","content":[{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}...]}]`。
- `"usage": {"include": true}` を付け、レスポンスの `usage.cost` を `cost_usd` に入れる（OpenRouter が USD を返す）。
- `json_schema` が与えられ、かつ `--strict-json` のときだけ `response_format: {"type":"json_schema", ...}` を送る。既定はコードフェンス抽出 + 1 回リトライ（モデル非依存で動く現行方式を維持）。
- 429 / 5xx / タイムアウトは指数バックオフで最大 3 回。タイムアウト既定 300 秒。
- 既定モデル `google/gemini-3.7-flash`（OpenRouter 上に存在し、`response_format` / `structured_outputs` 対応を確認済み）。
- API キー: `OPENROUTER_API_KEY`（環境変数 → `.env` → `~/.env.global` の順。`KEY="value"` のクォートを剥がす）。

**gemini.py**

- `google-genai` の Interactions API（`client.interactions.create(model=..., input=[...], response_format=schema)`）。
  実装の詳細と落とし穴は `try-gemini-agenticvideo/.claude/skills/gemini-video-analysis-skill/scripts/analyze_video.py` と
  レポート「実装上の注意」に従う（`create()` はキーワード引数、`response_mime_type` と `response_format` は併用不可、offset は `"48.00s"` 形式の文字列）。
- `MediaImage` は base64 の image ブロック。`MediaVideoClip` は Files API にアップロードした URI + `processing: {"type":"static","fps":..,"start_offset":..,"end_offset":..}` + `resolution`。
- アップロードはセッション内でキャッシュ（`<session>/uploads/<video_sha1>.json`。Files API は約 48 時間で失効するので `files.get` で ACTIVE を確認してから再利用）。
- `agentic` processing は使わない（検証で否定済み）。
- 既定モデル `gemini-3.7-flash`。コストは `avs/cost.py` の単価表から計算（入力 0.75 / 出力+思考 3.75 USD per 1M。単価表は日付付きで持ち、更新しやすくする）。
- 必要キー: `GEMINI_API_KEY`。**未設定時のエラー文で、必要なキーとバックエンドの対応を明示する**。
- `google-genai` は遅延 import。未導入時は「`pip install google-genai` が必要」と案内して終了。

CLI 共通オプション: `--backend openrouter|gemini`（既定 openrouter）、`--model`（省略時は backend の既定）、`--api-key`。

### 4.3 範囲計画（全区間カバー）

`plan_ranges.py` / `avs/ranges.py: plan_full_coverage()`

入力: overview 解析 JSON（`candidates[]`）、動画長、オプション。出力: 既存 config 形式の `ranges.json`。

アルゴリズム:

1. 候補を `start_sec` 順に並べ、`[0, duration]` にクリップ。重なり ≥ `--overlap-threshold`（0.5）はマージ。
2. `--max-range-sec`（既定 8）より長い範囲は等分割。`--min-range-sec`（既定 2）未満は隣とマージ。
3. 候補で覆われない隙間が `--min-gap-sec`（1.0）以上あれば `gap_NN` として `priority=low` の範囲で埋める（同じく ≤ max-range-sec に分割）。
4. **最後の範囲は必ず `end == duration`** にする（末尾の取りこぼし対策）。先頭も `start == 0`。
5. `--coverage`:
   - `full`（既定。動画長 ≤ `--full-coverage-max-sec` 600 のとき自動選択）: 全範囲を `--detail-fps`（5）で解析。
   - `priority`（600 秒超の既定）: high/medium は detail fps、low は `--low-fps`（1）。
   - `high-only`: high/medium のみ。low は解析しない。
6. 各 range に `note`（overview の `reason` + `title` を仮説として）、`priority`、`source: "overview" | "gap"` を付ける。`pad` は defaults に 1.0。

同じ `ranges.json` を tile 経路（`tile_video_frames.py --config`）と native 経路（`analyze.py --ranges`）の両方が読む。

### 4.4 プロンプトとドメイン定義

**プロンプト刷新**（すべて汎用語彙、`{{OBJECTIVE}}` 対応、コードフェンス JSON）

| ファイル | 変更点 |
|---------|--------|
| `overview.txt` | 「全区間をカバーする候補を 4〜8 秒単位で出す。何も起きていない区間も low で含める。空白を残さない」を明記。`reason` は次段の仮説になる旨 |
| `detail.txt` | 「重要な出来事」→「**この範囲で起きたことを出来事単位で漏れなく列挙**」。1 出来事 = 1 エントリ。「画面上の状態表示（数値・順位・ゲージ・アイコン・テキスト）の変化」「視覚エフェクトの出現・色や形の変化」は目的に関係しうる限り個別エントリにする。範囲先頭を 0 秒としない。既存の bounds / zoom_targets / hypothesis_verdict は維持 |
| `refine.txt` / `zoom.txt` | 現行維持。語彙の汎用性だけ点検 |
| `merge.txt` 【新規】 | 「入力に無い出来事を足さない。統合と重複排除だけ。confidence を格上げしない。細かい変化も独立項目として残す。importance は目的への寄与で付ける（rubric が与えられればそれに従う）。flags は引き継ぐ」 |
| `audio.txt` 【新規】 | 「聞こえたことだけ。映像から推測して補わない。BGM / 音声 / 効果音を秒数付きで」 |
| `chapters.txt` 【新規】 | 長尺の章立て把握用。大きな区切りを列挙 |

**`domain.json`**（`--domain` で全 CLI に渡す。任意）

```json
{
  "name": "ドメイン名",
  "description": "この種の動画の一般的な説明（任意）",
  "hud_notes": "画面上の状態表示の位置と読み方（任意）",
  "watchlist": ["注視すべき変化の一覧。例: 画面左下の数値表示の増減"],
  "vocabulary": {"用語": "映像上でどう見えるか"},
  "negatives": [
    {"name": "誤認されやすい事象名", "pattern": "正規表現", "window": [開始秒, 終了秒], "reason": "なぜ誤認しやすいか"}
  ],
  "importance_rubric": "high: …  medium: …  low: …"
}
```

- `avs/prompts.py` が `hud_notes / watchlist / vocabulary` を「## ドメインの手引き」として、`negatives` の `name` を
  「## 誤認されやすい事象（映像で明確に確認できた場合のみ書く）」としてプロンプト末尾に付ける。`importance_rubric` は merge プロンプトに入る。
- `negatives` の `pattern` はバリデータ（§4.5）が使う。`window` は任意。
- `examples/domain.example.json` は**抽象的なプレースホルダ**で書く（特定タイトルの固有名詞を入れない）。
- 既存の `--context`（自由テキスト）は残す。

### 4.5 後段バリデーション

`validate_analysis.py` / `avs/validate.py`。**削除はしない。フラグを付けるだけ。**

入力: detail の `*_analysis.json`（複数可。`--summary` で一括）、対応する manifest（あれば）、`--domain`。
出力: `<name>_validated.json`（元 JSON に各 event の `flags: []` と `confidence_adjusted` を追加）と `validation_report.json`（フラグ集計）。

| フラグ | 条件 | 効果 |
|-------|------|------|
| `negative_match` | `title+summary+visual` が `negatives[].pattern` にマッチ（`window` があれば時間も） | `confidence_adjusted = low`、要確認 |
| `no_cell_evidence` | tile 経路で `visual` にセルラベル引用（`F\d+` または `t=`）が 1 つも無い | 1 段階下げる |
| `evidence_out_of_range` | 引用された `t=` が範囲（pad 込み）の外 | 要確認 |
| `boundary` | `start_sec` または `end_sec` が範囲端から 1 フレーム間隔（1/fps）以内 | refine 候補 |
| `duration_outlier` | `end < start`、または長さ > `--max-event-sec`（既定 30） | 要確認 |
| `low_confidence` | 元の `confidence == low` | zoom / refine 候補 |
| `hypothesis_rejected` | `hypothesis_verdict == rejected` | overview の誤認候補として記録 |

### 4.6 統合・和集合・final.md

`merge_analyses.py` / `avs/merge.py`

1. **機械的統合**（LLM 不要）: 全範囲の events（`_validated.json` があればそれを優先）を時系列に並べ、
   範囲境界をまたぐ重複（時間重なり ≥ 0.5 かつタイトル類似度 ≥ 0.6、`difflib.SequenceMatcher`）を 1 件にまとめる。秒数は包含、confidence は**低い方**、`sources` に元範囲ラベルを列挙、`flags` は和集合。
2. **LLM 統合**（既定 ON、`--no-llm` で省略）: `merge.txt` + 機械統合結果 + objective + domain を渡し、`timeline.json` を得る。
   スキーマ: `{"overview": str, "timeline": [{"start_sec","end_sec","title","summary","importance","confidence","evidence","sources","flags"}]}`。
   エントリ数が `--llm-chunk`（既定 80）を超えるときは時間順に分割して呼び、結果を連結する。
3. **和集合** `--union A/merge/timeline.json B/merge/timeline.json`: 1 と同じ重複判定で機械的に合成し、`timeline_union.json` に書く。各項目に `runs: ["A","B"]` を残す。**既定は 1 回実行。** 人間から「精度が足りない」と指摘されたときに、同条件で再実行して和集合を取るのが標準の使い方（WORKFLOW §2.6）。
4. **final.md**: `timeline.json`（または union）から SKILL.md の最終出力形式を機械生成する（`--final-md`）。エージェントは必要なら注意点を追記する。

### 4.7 セッションと監査記録

`avs/session.py`

- セッション = `output/agentic_sessions/<video_stem>_<timestamp>/`。`session.json` に動画パス・長さ・backend・model・objective・domain のパス・各ステップの実行記録を持つ。
- 各 CLI は `--session DIR` を受け取る（省略時は出力パスから上方向に `session.json` を探す。見つからなければ記録しない）。
- **LLM 呼び出しごとに** `<name>_analysis.meta.json` を書く: backend、model、プロンプト本文（`prompt.txt` として別保存）、メディア一覧、usage、cost、latency、リトライ回数。同じ内容を `usage.jsonl` に 1 行追記する。
- `session_report.py` が `usage.jsonl` を集計し、呼び出し回数・トークン内訳・USD・所要時間と、**次アクション候補**
  （`negative_match` / `low_confidence` / `boundary` の件数と該当範囲、`zoom_targets` の一覧、失敗した範囲）を表示する。
- **失敗隔離とレジューム**: `analyze.py` は範囲ごとに例外を握って `batch_summary.json` の該当 `results[]` に `error` を書き、続行する。終了コードは全件失敗のときだけ 1（`--strict` で 1 件でも失敗なら 1）。出力 JSON が既に存在する範囲は既定でスキップ（`--force` で再解析）。

### 4.8 標準ドライバ `run_pipeline.py`

```bash
python scripts/run_pipeline.py --video video.mp4 --objective "…" [--domain domain.json] [--backend openrouter|gemini] [--input tile|native] [--session DIR]
```

内部で行うこと（各ステップは出力が存在すればスキップ = 再実行で再開）:

1. セッション作成、`ffprobe` で長さ取得。
2. 全体把握: 動画長 ≤ 600 秒なら `fps=--overview-fps`（既定 1）で全編タイル化 → `overview.txt`。600 秒超なら `chapters.txt` で章立て（fps 0.1〜0.2）→ 章ごとに overview（config モード）。native 入力ならタイル化せず動画全体を fps 1 で送る。
3. `plan_full_coverage()` で `ranges/ranges.json` を作る。
4. tile 入力なら `--config` で一括タイル化。native ならスキップ。
5. `detail.txt` で範囲ごとに解析（`--jobs` 既定 4）。
6. バリデーション。
7. 統合 → `merge/timeline.json` → `final.md`。
8. `session_report` を表示し、次アクション候補（ズーム・精密確認・再実行）を提案して終了。

ドライバはプリミティブ CLI の `main()` を関数として呼ぶのではなく、`avs/` の関数を直接呼ぶ（subprocess を挟まない）。
`--input native` は `--backend gemini` を要求する（openrouter は動画クリップ非対応）。矛盾する指定はエラー。

### 4.9 音声と ROI クロップ

- `analyze_audio.py --video --session`（gemini 限定）: **必ず全編**を送る（範囲を絞るとフレームを見ずに音声だけから捏造した実測がある）。
  出力 `audio/audio_analysis.json`: `{"segments":[{"start_sec","end_sec","kind":"bgm|speech|sfx|other","description","confidence"}]}`。
  merge は `--audio` で受け取り、映像側の出来事と時間重なりが無い音声項目に `audio_unconfirmed` フラグを付けて `timeline` に `source: "audio"` で残す。採用判断はエージェント。
- `tile_video_frames.py` zoom モードに `--crop W:H:X:Y`（ffmpeg の crop 記法）と `--scale N`（整数倍拡大、既定 1）を追加。config の range にも `crop` / `scale` キーを許可。
  `domain.json` の `hud_notes` に「どこを切り出すと読めるか」を書けば、エージェントがそれを見て crop を指定できる（コードは領域を知らない）。

### 4.10 評価基盤 `eval/`

- `eval/score.py`: 正解データ JSON（`events[].match.window/any`、`negatives[].pattern/window`）と本スキルの出力（`timeline.json` / `timeline_union.json` / 範囲ごとの `_analysis.json`）を照合し、網羅率・カテゴリ別・誤認数・コスト（`usage.jsonl` から）を出す。`try-gemini-agenticvideo/src/score.py` の移植。出力形状のアダプタを 1 箇所にまとめる。
- `eval/union_recall.py`: 複数実行の和集合網羅率。
- `eval/README.md`: 正解データの作り方（`try-gemini-agenticvideo/docs/video-analysis-benchmark-method.md` を汎用語彙に書き直したもの）と、回帰手順（プロンプトや既定値を変えたら同一動画で 3 回以上回して sd 込みで比較）。
- `eval/ground_truth.example.json`: 抽象的な例。`eval/fixtures/` は git 管理外（実データはローカルで用意）。
- テスト（`tests/`）は pytest。ffmpeg で合成動画（色ベタ + 焼き込みタイムコード）を作り、API 無しで tiling / ranges / validate / merge / backends のリクエスト組み立てを検証する。バックエンドはモック。

---

## 5. フェーズ分割

依存順に P0 → P8。各フェーズは単独でコミットし、完了条件を満たしてから次へ進む。
**担当モデル**は統率者がサブエージェントに割り当てる目安（上位 = 構造判断や文章品質が要るもの、軽量 = 移植・定型）。

| 順 | フェーズ | 内容 | 担当 | 規模 |
|---|---------|------|------|------|
| P0 | パッケージ化 | `avs/` 分割、薄い CLI、UTF-8 出力、tests 骨組み。**挙動不変** | 上位 | 中 |
| P1 | バックエンド | `backends/`、OpenRouter 直叩き、Gemini（画像・クリップ・音声）、usage/cost、`analyze.py` 改名と `--ranges` 入力、meta 保存 | 上位 | 大 |
| P2 | 範囲計画と堅牢化 | `plan_ranges.py`、全区間カバー、末尾必須、失敗隔離、スキップ/`--force`、`session.py`、`session_report.py` | 軽量 | 中 |
| P3 | プロンプトとドメイン | 6 プロンプト刷新・新設、`domain.json` 注入、`domain.example.json` | 上位 | 中 |
| P4 | 検証・統合 | `validate_analysis.py`、`merge_analyses.py`（機械統合・LLM 統合・和集合・final.md）、`analyze_audio.py`、`--crop/--scale` | 統合は上位、他は軽量 | 大 |
| P5 | ドライバ | `run_pipeline.py`（長尺の章立て分岐を含む） | 上位 | 中 |
| P6 | 評価基盤 | `eval/` 移植と汎用化、`tests/` 拡充 | 軽量 | 中 |
| P7 | ドキュメント | SKILL.md 全面改訂（WORKFLOW.md を元に）、README、examples | 上位 | 中 |
| P8 | 検証 | 合成動画で全 CLI を通す。実 API で 1 本通し、`eval/` で正解データに対する網羅率を測る | 軽量（判定は統率者） | 小 |

各フェーズ完了時に、実装担当とは別のサブエージェント（軽量）でコードレビューを行い、指摘を反映してからコミットする。

### P0: パッケージ化

- 対象: `scripts/*` 全体、`tests/`
- 作業: §4.1 のとおり。`tests/conftest.py` に合成動画フィクスチャ（ffmpeg が無ければ skip）。`tests/test_tiling.py`（セルラベルと焼き込み時刻の一致は目視不要な範囲で: manifest の秒数計算）、`tests/test_ranges.py`（config 合成・重複マージ）、`tests/test_analysis.py`（`chunk_with_overlap`、`extract_json`、`merge_part_results`）。
- 完了条件: v1 SKILL.md のコマンド例（`--dry-run` を含む）が同じ出力を出す。manifest の JSON が改修前後で一致（フィールド順を除く）。pytest 緑。

### P1: バックエンド

- 対象: `avs/backends/*`、`avs/analysis.py`、`avs/cost.py`、`analyze.py`
- 作業: §4.2。`analyze.py` に `--backend / --model / --api-key / --strict-json / --ranges / --video --start --end --fps`（native 単一範囲）を追加。`--aitool` は削除。
  `MediaVideoClip` を openrouter に渡したら明確なエラー。tests: `urllib` をモックしてリクエスト JSON（画像の data URI、usage.include）を検証。Gemini は `genai` をモック。
- 完了条件: `--dry-run` でバックエンド・モデル・メディア数・プロンプトの概要が出る。実キーがあれば 1 タイルで疎通確認（統率者が実施）。`*_analysis.meta.json` と `usage.jsonl` が書かれる。

### P2: 範囲計画と堅牢化

- 対象: `avs/ranges.py`、`plan_ranges.py`、`avs/session.py`、`session_report.py`、`analyze.py`
- 作業: §4.3、§4.7。tests: 隙間埋め、末尾一致、長い候補の分割、coverage モードごとの fps、スキップ/`--force`、1 範囲失敗時の続行。
- 完了条件: 合成 overview JSON から ranges.json を生成し `[0, duration]` を隙間なく覆う。失敗注入（モックが例外を投げる）で他範囲が完了し `error` が記録される。

### P3: プロンプトとドメイン

- 対象: `prompts/*.txt`、`avs/prompts.py`、`examples/domain.example.json`
- 作業: §4.4。語彙は汎用。各プロンプトの JSON スキーマを `avs/prompts.py` にも Python dict として持ち（`--strict-json` と merge 用）、テキストと二重管理にならないよう **プロンプト内のスキーマ例は dict から生成して埋め込む**（`{{SCHEMA}}` プレースホルダ）。
- 完了条件: `--dry-run` で組み立てたプロンプトに objective / タイル読解 / ドメイン手引き / ネガティブ名 / スキーマが正しい順で含まれる。固有名詞の grep（ドメイン固有語）がゼロ。

### P4: 検証・統合・音声・クロップ

- 対象: `avs/validate.py`、`validate_analysis.py`、`avs/merge.py`、`merge_analyses.py`、`analyze_audio.py`、`avs/tiling.py`
- 作業: §4.5、§4.6、§4.9。tests: 各フラグの発火条件、境界またぎ重複の統合、和集合の `runs` 付与、final.md 生成、crop/scale の ffmpeg 引数。
- 完了条件: 合成 detail JSON 群から `timeline.json`（`--no-llm`）と `final.md` が生成される。`--union` が 2 実行を合成する。

### P5: ドライバ

- 対象: `run_pipeline.py`
- 作業: §4.8。各ステップの「存在すればスキップ」と、終了時の次アクション提案。長尺分岐は合成 12 分動画で確認。
- 完了条件: モックバックエンドで tile / native 両経路が最後まで走り、セッション配下に §7（WORKFLOW）の成果物が揃う。

### P6: 評価基盤

- 対象: `eval/*`、`tests/`
- 作業: §4.10。`score.py` は本スキルの出力形状を読む。README は汎用語彙。
- 完了条件: `eval/ground_truth.example.json` と合成出力で `score.py` が動く。

### P7: ドキュメント

- 対象: `SKILL.md`、`README.md`、`examples/`
- 作業: WORKFLOW.md を正として SKILL.md を書き直す（エージェント向けに簡潔、コマンドは 1 行）。README は人間向け。キー要件（`OPENROUTER_API_KEY` / `GEMINI_API_KEY`）とバックエンド対応表を両方に置く。
- 完了条件: SKILL.md のコマンドが全て実在するオプションのみで構成されている（`--help` と突き合わせるスクリプトを tests に置く）。

### P8: 検証

1. 合成動画（5 シーン × 色 + 焼き込みタイムコード、12 分）で `run_pipeline.py` をモックバックエンドで通す。
2. 実 API（`OPENROUTER_API_KEY`）で `try-gemini-agenticvideo/video/video.mp4` を 1 回通し、`eval/score.py` で正解データ（同リポジトリの `ground_truth/video.json` を `eval/fixtures/` にコピー）に対する網羅率・誤認数・コストを出す。
   **目標: 網羅率 ≥ 90%（v1 相当の tile 経路 92.6% と同水準）、誤認 ≤ 1 件、コスト ≤ $0.20。** 1 回約 $0.12〜0.16。実施前に統率者がユーザーに費用を確認する。
3. 余力があれば gemini バックエンド（native）でも 1 回。

#### P8 の結果（2026-09-03）

- 合成 30 秒動画・合成 12 分動画（章立て分岐）・実動画の 3 本で `run_pipeline.py --dry-run` が Step 0〜7 を通り、範囲計画が先頭 0 秒から末尾まで隙間なく生成された。
- 実 API（openrouter / tile、ドメイン定義なし、既定値のまま）を 1 回実行。呼び出し 10 回（overview 1、detail 8、merge 1）、実測コスト $0.158、所要約 3.5 分。失敗した範囲は無し。
- 27 事象の正解データに対して **24/27（88.9%）、誤認 0 件**。カテゴリ別は導入 5/5、状態変化 8/8、操作・技術 2/4、異常・事故 9/10。
- 見逃した 3 件はいずれも「視覚エフェクトの色の変化」「接触時の粒子エフェクト」で、範囲ごとの詳細解析の段階で出ていない（統合で落ちたのではない）。先行検証で 92.6% を出した構成はプロンプトにその種の変化を個別に記録する指示を持っていたが、本改修ではドメイン固有の指示を `domain.json` の `watchlist` に移したため、**ドメイン定義なしの素の状態では技術系の細かい変化を拾いにくい**。目標 90% には 1 件届かず、差はばらつき（先行検証の sd 3.7）の範囲内。
- LLM 統合が 8 件を 1 件にまとめる方向で落としたが、照合処理が `llm_dropped` フラグ付きで復元し、出来事は失われなかった。
- 次の検証候補: (a) 抽象語で書いた `watchlist`（エフェクトの色変化・接触時の粒子など）を持つ `domain.json` を付けて再実行し、技術系の網羅率が回復するか、(b) 同条件で 2 回目を回して和集合の効果を測る。

---

## 6. 互換性・リスク

### 破壊的変更（意図的）

- `analyze_tile_manifest.py` → `analyze.py` に改名。`--aitool` 削除。`aitool` は不要になる。
- 既定モデルが `google/gemini-3.5-flash` → `google/gemini-3.7-flash`。
- `--expect-json` は既定 ON（`--raw` で無効化）。
- config `defaults.pad` の推奨値 2 → 1.0（全区間カバーで隣接範囲が重なるため）。

### リスクと対策

| リスク | 対策 |
|-------|------|
| OpenRouter の `usage.cost` が返らないモデルがある | `cost_usd` を None 許容にし、`cost.py` の単価表でフォールバック。集計で「概算」と明示 |
| 巨大な base64 リクエストでタイムアウト | タイムアウト 300 秒、`--max-tiles-per-call` 既定 8 を維持、リトライ |
| Gemini Files API の失効・アップロード待ち | ACTIVE 確認とポーリング、セッション内キャッシュ |
| 全区間カバーで長尺のコストが膨らむ | 600 秒超は `priority` 既定。`session_report` で事前見積もり（範囲数 × fps × 平均トークン）を出す |
| LLM 統合が出来事を落とす・足す | 機械統合の結果を必ず保存し、LLM 統合後に**件数と時間範囲の差分**を `validation_report` に記録。`--no-llm` で回避可能 |
| プロンプトの汎用化で検出率が下がる | P8 で正解データに対して測る。下がれば watchlist を `domain.json` 側に寄せて再測定 |
| リファクタで挙動が変わる | P0 は挙動不変を完了条件にし、manifest / dry-run 出力の一致で確認 |

---

## 7. 決定事項と保留

- 和集合の既定は **1 回**。`--union` はオプション。
- 中長期テーマ（自己一貫性、クロスモデル検証、シーンチェンジ分割、agentic モード再評価）は**保留**。本書には含めない。
- ドメイン固有の知識（特定タイトルの用語・HUD 位置・誤認語）はスキルに入れない。`domain.json` の例も抽象語で書く。
- 実 API を使う検証（P8-2）は費用が発生するため、実施前にユーザー確認を取る。
