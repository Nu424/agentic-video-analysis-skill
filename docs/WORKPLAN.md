# 改修作業書: agentic-video-analysis-skill

作成日: 2026-07-04

## 1. 目的

「粗く見て、気になった所だけ細かく見る」という現行アーキテクチャ(2スクリプト+3プロンプト+SKILL.md)を維持したまま、以下を達成する。

- **精度**: 出力の構造化、仮説検証型のdetail解析、秒数推定の厳密化、高解像度ズーム
- **汎用性**: 解析目的のパラメータ化、長時間動画への対応、長尺向けタイムスタンプ表記
- **効率**: 解析の並列実行、画像サイズ・品質の既定値見直し
- **保守性**: 共通処理の `common.py` への抽出、重複コードの統合

### スコープ外(今回やらない)

- 音声解析(文字起こし併用)
- 動画タイプ別プリセット・自動判定
- シーンチェンジ検出によるサンプリング
- タイル化のキャッシュ・冪等化

## 2. 改修後の構成

```text
skills/agentic-video-analysis-skill/
  SKILL.md                  # 手順書(長尺対応・ズーム・新オプションを反映)
  prompts/
    overview.txt            # JSON出力・{{OBJECTIVE}}対応に刷新
    detail.txt              # 同上+仮説検証型に刷新
    refine.txt              # 同上
    zoom.txt                # 【新規】高解像度フレームの細部確認用
  scripts/
    common.py               # 【新規】共有ユーティリティ
    tile_video_frames.py    # タイル化CLI(+zoomモード)
    analyze_tile_manifest.py# 解析CLI(+並列、分割、コンテキスト注入、JSON検証)
    requirements.txt
  examples/
    ranges.example.json     # note フィールドを追加した雛形に更新
```

原則: **CLIは2本のまま**。既存のオプション・manifest互換は維持し、新機能はすべて追加オプションで導入する(破壊的変更は §5.1 のmanifest形状統一のみ、影響範囲は本スキル内で完結)。

## 3. フェーズ分割と作業項目

依存関係順に P0 → P5。各フェーズ完了時点で動作する状態を保つ。

---

### P0: 共通基盤の整理(挙動変更なしのリファクタリング)

**対象**: `scripts/common.py`(新規)、既存2スクリプト

1. `common.py` を新設し、以下を移動・集約する。
   - `run_command()`(subprocess実行+エラー整形)
   - `probe_duration_sec()` / `probe_video_size()`
   - `format_float_for_path()`
   - `load_font()`
   - JSON読み書きヘルパ(`read_json(path)` / `write_json(path, data)`: encoding="utf-8", ensure_ascii=False, indent=2 を統一)
   - タイムスタンプ整形 `format_timestamp(sec, long_form: bool)`(P3で使用。`38.0` → `"38.0s"` / `1234.5` → `"20:34.5"`)
2. 両スクリプトから `from common import ...` で参照する(スクリプトと同ディレクトリなので追加設定不要)。
3. `tile_video_frames.py` 内の `build_single_metadata()` / `build_multi_metadata()` を `build_manifest()` 1本に統合する。
   - manifest形状は「常に multi 形」(`tiling.tile_count` + `tiles[]` 配列)に統一し、`version: 2` にする。
   - `approach` は `"agentic_video_frame_tiles"` に統一。
   - `analyze_tile_manifest.py` は `tiles` と `extraction` しか読まないため影響なし。

**完了条件**: 既存のREADME/SKILL.mdのコマンド例がそのまま動き、単一タイル時のmanifestが `tiles` 配列1件の形で出力される。

---

### P1: プロンプト刷新+コンテキスト注入(項目 1, 2, 3, 6, 7)

**対象**: `prompts/*.txt`、`analyze_tile_manifest.py`

#### P1-1. タイル読解ルールをスクリプト側に集約(項目6)

3プロンプトの冒頭にある重複記述(「タイル画像である」「セルラベルの説明」)を削除し、`build_tile_context()` に以下を集約する。

- セルの読み順: 左→右、上→下で時系列
- タイル間の順序: Tile 0 → Tile 1 … の順で連続
- 隣接セルの内容を混同しない。まず各タイルを走査し、その後で統合する
- `t=<秒>s` は動画内の絶対時刻(既存記述)
- 複数タイルにまたがる出来事は統合して1件にする(既存記述)

#### P1-2. 解析目的のパラメータ化(項目7)

- 3プロンプト(+新設zoom.txt)の冒頭に `{{OBJECTIVE}}` プレースホルダを設ける。
  例: `解析の目的: {{OBJECTIVE}}`
- `analyze_tile_manifest.py` に `--objective <テキストまたはファイルパス>` を追加。
  - 置換して渡す。未指定時の既定値: 「動画の見どころ候補の抽出」
  - プレースホルダが無いプロンプト(ユーザー自作)ではそのまま何もしない。
- 併せて汎用の `--context <テキストまたはファイルパス>` を追加(プロンプト末尾に「## 追加コンテキスト」として付加)。P2-3の仮説注入と同じ経路を使う。

#### P1-3. JSON構造化出力(項目1)

3プロンプトの出力指示を「コードフェンス付きJSONのみを出力」に変更し、スキーマを明記する。

**overview.txt** の出力スキーマ:

```json
{
  "summary": "動画全体の概要(2〜4文)",
  "candidates": [
    {
      "label": "candidate_a",
      "start_sec": 38.0,
      "end_sec": 46.0,
      "start_bounds": [36.0, 38.0],
      "end_bounds": [44.0, 46.0],
      "title": "短いタイトル",
      "evidence": ["F19 t=38.0s に◯◯が見える"],
      "priority": "high | medium | low",
      "needs_followup": true,
      "reason": "追加確認すべき理由"
    }
  ]
}
```

**detail.txt** の出力スキーマ:

```json
{
  "events": [
    {
      "start_sec": 39.0, "end_sec": 44.2,
      "start_bounds": [38.8, 39.0], "end_bounds": [44.2, 44.4],
      "title": "…", "summary": "…",
      "visual": ["F5 t=39.0s: …"],
      "confidence": "high | medium | low",
      "zoom_targets": [39.4]
    }
  ],
  "hypothesis_verdict": "confirmed | partially | rejected | n/a",
  "notes": "誤認しやすい点など"
}
```

- `zoom_targets`: 細部(文字・UI等)が判読できず高解像度確認が有効な時刻。P3のzoomモードの入力になる。
- `hypothesis_verdict`: P2-3で注入する仮説への回答。仮説なしなら `"n/a"`。

**refine.txt** の出力スキーマ:

```json
{
  "start_sec": 38.8, "end_sec": 44.6,
  "evidence": ["…"],
  "suggested_pad_before_sec": 1.0,
  "suggested_pad_after_sec": 0.5,
  "needs_more_review": false,
  "remaining_questions": []
}
```

#### P1-4. 秒数推定ルール(項目3)

3プロンプト共通の判断方針に追記する。

- 「開始・終了の断定は、あるフレームで確認できた時刻までしかできない。`start_bounds` / `end_bounds` に『直前フレームの時刻(その時点では未発生)』と『当該フレームの時刻(発生を確認)』を必ず書く」
- 「`start_sec` / `end_sec` はその区間内の最良推定値とする」

#### P1-5. JSON検証+リトライ(項目1)

`analyze_tile_manifest.py` に `--expect-json` フラグを追加。

- aitool出力からコードフェンスを剥がして `json.loads` を試みる。
- 成功: `<name>_analysis.json` として整形保存(生テキストも `<name>_analysis.raw.txt` に保持)。
- 失敗: プロンプト末尾に「前回の出力はJSONとして不正だった。JSONのみを出力せよ」を付加して**1回だけ**リトライ。
- リトライも失敗: 生テキストを保存して警告を出し、処理は継続(バッチ全体を止めない)。終了時に失敗件数を報告。

**完了条件**: `--dry-run` で組み立てられるプロンプトに、目的・読解ルール・スキーマが正しく含まれる。不正JSONを返すダミーコマンド(`--aitool` 差し替え)でリトライ動作を確認できる。

---

### P2: 解析CLIの機能強化(項目 2, 9, 11)

**対象**: `analyze_tile_manifest.py`、`tile_video_frames.py`(summaryへのnote伝搬)、`examples/ranges.example.json`

#### P2-1. 並列実行(項目11)

- `--jobs N`(既定1)を追加し、複数manifestを `ThreadPoolExecutor` で並列解析する。
- 出力パスの衝突チェックを事前に行う(同名出力が複数あればエラー)。
- 進捗表示は「開始/完了」を行単位で出す(並列時の出力混線を避けるため、1行に集約して都度flush)。

#### P2-2. 1呼び出しあたりのタイル数上限(項目9)

- `--max-tiles-per-call N`(既定8)を追加。
- manifestのタイル数がNを超える場合、時系列順にチャンク分割して複数回呼び出す。
  - 各パートのプロンプトには `build_tile_context()` で「全体のうち何秒〜何秒のパートか(パート i/n)」を明示。
  - 出力: `<name>_analysis_part00.json` … を保存後、機械的に統合した `<name>_analysis.json` を生成(overview: `candidates` 配列を連結、summaryは各パートのものを配列で保持。detail/refine: 同様に配列連結)。
- 分割はAPI呼び出し回数が増える=偽陰性境界がパート境界に出やすいため、チャンクは1タイル分オーバーラップさせる(パート境界の出来事の取りこぼし防止)。統合時に同一候補が重複しうる旨をSKILL.mdに明記(判断はエージェントに委ねる)。

#### P2-3. 仮説注入(項目2)

- config(ranges.json)の range に任意フィールド `note` を追加できるようにする。
  - `tile_video_frames.py` の configモード: `note` を `batch_summary.json` の `results[]` に透過コピーする(マージ時は `_` 連結でなく `" / "` 連結)。
- `analyze_tile_manifest.py` の `--summary` モード: 各rangeの `note` があれば、プロンプト末尾に以下を付加する。

  ```text
  ## 事前の仮説(全体把握での観察)
  <note>

  この仮説を映像上の根拠で確認・反証し、hypothesis_verdict に回答すること。
  仮説に引きずられず、見えている事実を優先すること。
  ```

- `examples/ranges.example.json` に `note` 付きの例を反映。

**完了条件**: `--summary --jobs 4 --dry-run` で並列予定・分割予定・注入されるnoteが確認できる。タイル数9以上のmanifestで分割・統合出力が生成される。

---

### P3: タイル化CLIの機能強化(項目 4, 10, 14)

**対象**: `tile_video_frames.py`、`prompts/zoom.txt`(新規)

#### P3-1. zoomモード(項目4)

- `--timestamps 38.5,40.2`(`--start/--end/--fps` と排他)を追加。
- 各時刻の1フレームを**フル解像度**(リサイズなし、または `--width` 指定時のみ縮小)で抽出し、1枚=1画像として保存する。タイルグリッドには載せない。
- 各画像の下部に既存と同形式のラベルバー(`F<n> t=<秒>s`)を描画する。
- manifestは既存互換の形(`tiles[]` に1フレーム=1エントリ、`approach: "agentic_video_frame_zoom"`)で出力し、`analyze_tile_manifest.py` がそのまま読めるようにする。
- configモードのrangeにも `timestamps` キーを許可する(detailの `zoom_targets` をconfig化して一括ズームできるように)。

#### P3-2. zoom.txt(新規プロンプト)

- 目的: 特定時刻のフレームの細部(文字・UI・小さなオブジェクト)の判読。
- `{{OBJECTIVE}}` 対応、JSON出力:

```json
{
  "frames": [
    {
      "timestamp_sec": 38.5,
      "readable_text": ["画面上で判読できた文字列"],
      "details": "細部の観察",
      "unreadable": ["判読を試みたが読めなかった要素"]
    }
  ]
}
```

- 「読めないものを推測で補完しない。判読できなかったものは unreadable に書く」を明記。

#### P3-3. 画像サイズ・品質の既定値見直し(項目14)

- JPEG品質の既定値: 90 → **80**(オプションで従来値に戻せる)。
- タイル目標サイズの既定: 1920×1080 → **1600×900**。
- 抽出幅 `--width` 既定640は据え置き(セル幅上限がボトルネックのため)。
- zoomモードは品質90・リサイズなしを既定とする(細部判読が目的のため)。
- 変更前後で同一範囲のタイルを目視比較し、ラベルと画面内容の判読性が保たれることを確認してから確定する。

**完了条件**: zoomモードで生成した画像+manifestが `analyze_tile_manifest.py --dry-run` で解析コマンドになる。600秒超の範囲で `m:ss` ラベルが描画される。

---

### P4: ドキュメント更新(項目9の運用面+全体反映)

**対象**: `SKILL.md`、`README.md`、`examples/ranges.example.json`

1. **長尺動画の階層化手順(項目9)** を SKILL.md Step 1 に追加。
   - 目安: 動画長 ≤ 10分 → 現行どおり fps=0.5 で一括。
   - 10分超 → まず fps=0.1〜0.2 で「章立て把握」→ 章ごとに fps=0.5 のoverviewを実施(configモードで章を範囲として一括処理)。
   - `--max-tiles-per-call` の既定(8)と、分割時の重複候補の扱いを記載。
2. **Step 2.5「ズーム確認」** を追加(Step 3 の手前、任意)。
   - detailの `zoom_targets` / `confidence: low` かつ文字・細部起因の候補に対し、`--timestamps` + `zoom.txt` で確認する手順。
   - fps早見表に「ズーム: 特定時刻のみ・フル解像度」の行を追加。
3. **新オプションの反映**: パラメータ早見表・スクリプトリファレンスに `--objective` / `--context` / `--expect-json` / `--jobs` / `--max-tiles-per-call` / `--timestamps` を追記。
4. **JSON運用の反映**: notes/candidates.md の雛形を、overview JSONの `candidates` から機械的に作れる形に更新。`hypothesis_verdict` / `zoom_targets` を Step 2 ③判断の基準に組み込む。
5. **README.md**: 実行の流れ図にズームを追加、fps表更新、成果物ディレクトリに `zooms/` を追加。
6. セッション構成に `zooms/` ディレクトリを追加:

   ```text
   output/agentic_sessions/<video_stem>_<timestamp>/
     overview/ candidates/ zooms/ refinements/ notes/ final.md
   ```

---

### P5: 検証

APIを使わない検証を基本とし、最後に実APIで1本通す。

1. **合成テスト動画の生成**(ffmpegのみで作成、リポジトリには含めない):

   ```bash
   # 5シーン×違う色+焼き込みタイムコード、計12分(分割の検証用)
   ffmpeg -f lavfi -i "color=red:size=640x360:duration=144" ... (concat) \
     -vf "drawtext=text='%{pts\:hms}':fontsize=48" test_video.mp4
   ```

   (実際の生成コマンドは検証時に組み立てる。焼き込みタイムコードとセルラベルの一致確認が目的)
2. **チェックリスト**:
   - [ ] P0後: 既存コマンド例がそのまま動く/manifestがversion 2・tiles配列形
   - [ ] タイル画像のセルラベルと焼き込みタイムコードが一致(通常・zoomの2通り)
   - [ ] `--config` + `note` → `batch_summary.json` に伝搬 → `--summary --dry-run` のプロンプトに注入される
   - [ ] `{{OBJECTIVE}}` が `--objective` で置換される(未指定時は既定文言)
   - [ ] タイル数9以上で分割され、part出力+統合JSONが生成される(1タイルオーバーラップ)
   - [ ] `--jobs 4` で並列実行され、出力の欠落・混線がない
   - [ ] 不正JSON→リトライ→失敗時も継続、の一連の挙動(ダミーaitoolで確認)
   - [ ] 品質80/1600×900のタイルで判読性が保たれる(目視)
3. **実API検証(任意・要 OPENROUTER_API_KEY)**: 実動画1本でStep 1→2→ズーム→final.mdまで通し、JSONがそのままパースできることを確認。

## 4. 実施順序と目安

| 順 | フェーズ | 主な成果物 | 規模感 |
| --- | --- | --- | --- |
| 1 | P0 | common.py、manifest統一 | 小(移動中心) |
| 2 | P1 | プロンプト3本刷新、--objective/--context/--expect-json | 中 |
| 3 | P2 | --jobs/--max-tiles-per-call/note注入 | 中 |
| 4 | P3 | zoomモード、zoom.txt、既定値変更 | 中 |
| 5 | P4 | SKILL.md/README更新 | 小〜中 |
| 6 | P5 | 検証・修正 | 小 |

各フェーズごとにコミットを分ける(P0はリファクタリング単独コミットとし、挙動変更と混ぜない)。

## 5. 互換性・リスク

### 5.1 破壊的変更(意図的)

- **manifest形状の統一(version 2)**: 単一タイル時も `tiles[]` 配列形になる。消費者は本スキルの `analyze_tile_manifest.py` のみで、同スクリプトは元々両形を透過的に扱うため実影響なし。
- **プロンプト出力がJSONになる**: 過去セッションの `*_analysis.txt`(自由テキスト)とは形式が変わる。過去成果物は読み取り専用なので混在しても問題ない。
- **タイル品質・サイズの既定値変更**: 見た目が変わる。従来値はオプションで再現可能。

### 5.2 リスクと対策

| リスク | 対策 |
| --- | --- |
| モデルがJSON指示を守らない | リトライ1回+生テキスト保存で継続(P1-5)。スキーマは浅く保つ |
| 分割解析でパート境界の出来事を取りこぼす | 1タイルオーバーラップ+SKILL.mdで重複統合を指示(P2-2) |
| 仮説注入が誘導バイアスになる | 注入文に「仮説に引きずられない」を明記、verdictで反証を許容(P2-3) |
| ラベル形式でモデルが混乱 | 出力は常に秒数値に固定(P3-3) |
| 品質80で細部が潰れる | zoomモードで担保。目視確認してから既定値を確定(P3-4) |
