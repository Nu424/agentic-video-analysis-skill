#!/usr/bin/env python
"""標準ドライバ: 全体把握 → 範囲計画 → タイル化 → 詳細解析 → 検証 → 統合 → レポートを1コマンドで通すCLI。

  python skills/agentic-video-analysis-skill/scripts/run_pipeline.py \
    --video video.mp4 --objective "動画の見どころ候補の抽出" --domain notes/domain.json

ネイティブ動画クリップ経路（gemini 限定）:
  python skills/agentic-video-analysis-skill/scripts/run_pipeline.py \
    --video video.mp4 --backend gemini --input native

各ステップは主要出力が既にあればスキップするので、途中で止まっても同じコマンドで再開できる
（`--force` で全ステップ再実行）。動画長が --full-coverage-max-sec（既定600秒）を超えると、
章立て（chapters.txt）→ 章ごとの全体把握 → coverage priority の長尺分岐に入る。

出力はセッションディレクトリ（既定 output/agentic_sessions/<動画名>_<日時>/）配下:
  session.json / usage.jsonl / overview/ / ranges/ / merge/ / final.md / notes/

処理本体は avs/pipeline.py にある。個別のステップだけ走らせたいときは
tile_video_frames.py / plan_ranges.py / analyze.py / validate_analysis.py /
merge_analyses.py / session_report.py を直接使う（ドライバはこれらと同じ関数を呼ぶ）。
"""

from __future__ import annotations

import argparse
import sys

from avs.backends import BACKEND_NAMES
from avs.common import configure_utf8_stdout
from avs.pipeline import (
    DEFAULT_BACKEND,
    DEFAULT_CHAPTERS_FPS,
    DEFAULT_DETAIL_FPS,
    DEFAULT_FRAMES_PER_TILE,
    DEFAULT_INPUT_MODE,
    DEFAULT_JOBS,
    DEFAULT_LOW_FPS,
    DEFAULT_MAX_RANGE_SEC,
    DEFAULT_OVERVIEW_FPS,
    DEFAULT_PAD,
    DEFAULT_SESSION_ROOT,
    INPUT_MODES,
    PipelineOptions,
    run_pipeline,
)
from avs.prompts import DEFAULT_OBJECTIVE
from avs.ranges import DEFAULT_FULL_COVERAGE_MAX_SEC


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "動画を1コマンドで解析します（全体把握 → 範囲計画 → タイル化 → 詳細解析 → "
            "検証 → 統合 → レポート）。各ステップは出力が既にあればスキップします。"
        )
    )
    parser.add_argument("-v", "--video", required=True, help="入力動画パス")
    parser.add_argument(
        "--objective",
        default=None,
        help=f"解析の目的（テキスト or ファイルパス）。既定: {DEFAULT_OBJECTIVE}",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help=(
            "ドメイン定義JSON（examples/domain.example.json の形式）。"
            "セッションの notes/domain.json にコピーして全ステップで使う"
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
        "--input",
        dest="input_mode",
        choices=list(INPUT_MODES),
        default=DEFAULT_INPUT_MODE,
        help=(
            f"入力形式（既定: {DEFAULT_INPUT_MODE}）。tile はフレームタイル画像、"
            "native はネイティブ動画クリップ（--backend gemini が必要）"
        ),
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "セッションディレクトリ。既存を指定すると再開する（完了済みステップはスキップ）。"
            "省略時は <--session-root>/<動画名>_<日時>/ を新規作成する"
        ),
    )
    parser.add_argument(
        "--session-root",
        default=DEFAULT_SESSION_ROOT,
        help=f"セッションを作る親ディレクトリ（既定: {DEFAULT_SESSION_ROOT}）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="使用モデル。省略時はバックエンドの既定（openrouter: google/gemini-3.7-flash）",
    )
    parser.add_argument(
        "--overview-fps",
        type=float,
        default=DEFAULT_OVERVIEW_FPS,
        help=f"全体把握のfps（既定: {DEFAULT_OVERVIEW_FPS:g}）",
    )
    parser.add_argument(
        "--detail-fps",
        type=float,
        default=DEFAULT_DETAIL_FPS,
        help=f"詳細解析のfps（high/medium 範囲。既定: {DEFAULT_DETAIL_FPS:g}）",
    )
    parser.add_argument(
        "--low-fps",
        type=float,
        default=DEFAULT_LOW_FPS,
        help=f"coverage priority のとき low 範囲に使うfps（既定: {DEFAULT_LOW_FPS:g}）",
    )
    parser.add_argument(
        "--chapters-fps",
        type=float,
        default=DEFAULT_CHAPTERS_FPS,
        help=f"長尺の章立てに使うfps（既定: {DEFAULT_CHAPTERS_FPS:g}）",
    )
    parser.add_argument(
        "--coverage",
        choices=["auto", "full", "priority", "high-only"],
        default="auto",
        help=(
            "範囲網羅の方式（既定: auto。動画長が --full-coverage-max-sec 以下なら full、"
            "超えるなら priority）"
        ),
    )
    parser.add_argument(
        "--max-range-sec",
        type=float,
        default=DEFAULT_MAX_RANGE_SEC,
        help=f"1範囲の最大秒数（既定: {DEFAULT_MAX_RANGE_SEC:g}）",
    )
    parser.add_argument(
        "--pad",
        type=float,
        default=DEFAULT_PAD,
        help=f"範囲の前後に足す秒数（既定: {DEFAULT_PAD:g}）",
    )
    parser.add_argument(
        "--frames-per-tile",
        type=int,
        default=DEFAULT_FRAMES_PER_TILE,
        help=f"1タイルあたりの最大フレーム数（既定: {DEFAULT_FRAMES_PER_TILE}）",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"詳細解析の並列ワーカー数（既定: {DEFAULT_JOBS}）",
    )
    parser.add_argument(
        "--full-coverage-max-sec",
        type=float,
        default=DEFAULT_FULL_COVERAGE_MAX_SEC,
        help=(
            "この秒数を超えたら長尺分岐（章立て + coverage priority）に入る"
            f"（既定: {DEFAULT_FULL_COVERAGE_MAX_SEC:g}）"
        ),
    )
    parser.add_argument(
        "--no-llm-merge",
        action="store_true",
        help="統合でLLMを使わず、機械統合の結果をそのまま timeline.json にする",
    )
    parser.add_argument(
        "--strict-json",
        action="store_true",
        help="JSONスキーマが与えられた呼び出しで構造化出力（response_format）を要求する",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="出力が既にあるステップもすべて再実行する（既定はスキップして再開）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="各ステップの実行予定（パス・件数・fps・呼び出し予定）を表示し、APIを呼ばない（タイル化は実行する）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    return run_pipeline(PipelineOptions.from_namespace(parse_args(argv)))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
