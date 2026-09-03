#!/usr/bin/env python
"""タイル画像またはネイティブ動画クリップを LLM で解析するCLI。

バックエンドは `--backend openrouter|gemini`（既定 openrouter）。
`aitool` は不要（OpenRouter を直接叩く / Gemini は google-genai）。

入力は 3 通り:

- タイル manifest: `--manifest` / `--summary`（1 manifest = 原則 1 回の呼び出し。
  タイル数が多い場合は `--max-tiles-per-call` で時系列にチャンク分割）
- 範囲 config:     `--ranges ranges.json`（範囲ごとにネイティブ動画クリップを 1 本送る。gemini 限定）
- 単一範囲:        `--video video.mp4 --start 48 --end 54 --fps 10`（同上）

単一 manifest:
  python skills/agentic-video-analysis-skill/scripts/analyze.py \
    --manifest output/agentic_sessions/example/overview/full/manifest.json \
    --prompt skills/agentic-video-analysis-skill/prompts/overview.txt \
    --objective "動画の見どころ候補の抽出" --session output/agentic_sessions/example

複数 manifest（タイル化 summary から自動列挙）:
  python skills/agentic-video-analysis-skill/scripts/analyze.py \
    --summary output/agentic_sessions/example/ranges/batch_summary.json \
    --prompt skills/agentic-video-analysis-skill/prompts/detail.txt --jobs 4

ネイティブ動画クリップ（gemini）:
  python skills/agentic-video-analysis-skill/scripts/analyze.py \
    --ranges output/agentic_sessions/example/ranges/ranges.json --backend gemini \
    --prompt skills/agentic-video-analysis-skill/prompts/detail.txt \
    --output-dir output/agentic_sessions/example/ranges

出力（<name>_analysis を base として）:
  base.json / base.raw.txt      解析結果（--raw なら base.txt のみ）
  base.prompt.txt               実際に送ったプロンプト
  base.meta.json                backend / model / メディア / usage / cost / latency / retries
  <session>/usage.jsonl         上記 + name を 1 行追記（--session 指定時。省略時は出力先から自動検出）

主要出力（base.json / --raw なら base.txt）が既に存在するジョブは既定でスキップする
（--force で再解析）。範囲ごとに失敗を隔離して続行し、終了コードは既定では全件失敗のときだけ1
（--strict で1件でも失敗したら1）。--summary 経由なら batch_summary.json の results[] に
analysis_status / analysis_error を書き戻す。--ranges 経由なら output_dir 直下に
analysis_summary.json（results[]{label,output,status,error?}）を書く。

処理本体は avs/analysis.py（解析）、avs/backends/（バックエンド）、
avs/prompts.py（プロンプト組み立て）にある。
"""

from __future__ import annotations

import argparse
import sys

from avs.analysis import (
    DEFAULT_BACKEND,
    DEFAULT_CLIP_FPS,
    DEFAULT_MAX_TILES_PER_CALL,
    AnalyzeOptions,
    run_analysis,
)
from avs.backends import BACKEND_NAMES
from avs.common import configure_utf8_stdout
from avs.prompts import DEFAULT_OBJECTIVE, OBJECTIVE_PLACEHOLDER


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "タイル manifest の全タイル、または動画クリップを LLM に渡して解析します。"
            "複数 manifest / 複数範囲を並列処理できます。"
        )
    )
    parser.add_argument(
        "-m",
        "--manifest",
        nargs="+",
        default=None,
        help="タイル manifest.json のパス（複数指定可）",
    )
    parser.add_argument(
        "-s",
        "--summary",
        default=None,
        help="タイル化の batch_summary.json。results[].manifest_path から manifest を自動列挙する",
    )
    parser.add_argument(
        "--ranges",
        default=None,
        help="範囲 config（ranges.json）。範囲ごとにネイティブ動画クリップを送る（--backend gemini 限定）",
    )
    parser.add_argument(
        "--video",
        default=None,
        help="単一範囲をネイティブ動画クリップで解析する場合の動画パス（--backend gemini 限定）",
    )
    parser.add_argument("--start", type=float, default=None, help="--video のときの開始秒（既定: 0）")
    parser.add_argument("--end", type=float, default=None, help="--video のときの終了秒（既定: 動画末尾）")
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help=f"--video / --ranges のときのサンプリング fps（既定: {DEFAULT_CLIP_FPS:g}）",
    )
    parser.add_argument("-p", "--prompt", required=True, help="プロンプトテキストファイルのパス")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="解析結果の出力パス（対象が1件のときのみ有効）。省略時は manifest 横に自動命名",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="出力をまとめるディレクトリ。各対象を <名前>_analysis.* で出力（--ranges では必須）",
    )
    parser.add_argument(
        "--objective",
        default=None,
        help=f"解析の目的（テキスト or ファイルパス）。プロンプト内の {OBJECTIVE_PLACEHOLDER} を置換。既定: {DEFAULT_OBJECTIVE}",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="追加コンテキスト（テキスト or ファイルパス）。プロンプト末尾に付加する",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help=(
            "ドメイン定義JSON（examples/domain.example.json の形式）。"
            "手引きと誤認されやすい事象をプロンプトに付ける"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=list(BACKEND_NAMES),
        default=DEFAULT_BACKEND,
        help=(
            f"使用するバックエンド（既定: {DEFAULT_BACKEND}）。"
            "openrouter は OPENROUTER_API_KEY、gemini は GEMINI_API_KEY と google-genai が必要"
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="使用モデル。省略時はバックエンドの既定（openrouter: google/gemini-3.7-flash / gemini: gemini-3.7-flash）",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "APIキー。省略時は 環境変数 -> ./.env -> ~/.env.global の順に探す"
            "（非推奨。プロセス一覧に露出しうるため環境変数か .env を推奨）"
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="出力をJSONとして検証しない（既定はJSON検証 + 失敗時1回リトライ）",
    )
    parser.add_argument(
        "--strict-json",
        action="store_true",
        help="JSONスキーマが与えられた呼び出しで構造化出力（response_format）を要求する",
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "セッションディレクトリ。指定すると <session>/usage.jsonl に呼び出し記録を追記する。"
            "省略時は出力先から上方向に session.json を探し、見つかればそれを使う"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="主要出力（<name>_analysis.json 等）が既に存在するジョブも再解析する（既定はスキップ）",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="1件でも解析に失敗したら終了コード1にする（既定は全件失敗のときだけ1）",
    )
    parser.add_argument(
        "--max-tiles-per-call",
        type=int,
        default=DEFAULT_MAX_TILES_PER_CALL,
        help=f"1回の呼び出しに渡すタイル数の上限（既定: {DEFAULT_MAX_TILES_PER_CALL}）。超過分は時系列で分割",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="複数対象を並列解析するワーカー数（既定: 1）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="backend / model / メディア / プロンプト先頭200字 / 出力先を表示するだけで API を呼ばない",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    return run_analysis(AnalyzeOptions.from_namespace(parse_args(argv)))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
