#!/usr/bin/env python
"""タイル manifest を aitool recognize-image で解析するCLI。

1つの manifest = 原則1回の API 呼び出し（含まれる全タイルをまとめて渡す）。
タイル数が多い場合は時系列順にチャンク分割して複数回呼び出す（--max-tiles-per-call）。
複数 manifest をまとめて／並列に処理できる（--jobs）。

単一 manifest:
  python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py \
    --manifest output/agentic_sessions/example/overview/full/manifest.json \
    --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt \
    --output output/agentic_sessions/example/overview/full_analysis.txt

複数 manifest（タイル化 summary から自動列挙）:
  python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py \
    --summary output/agentic_sessions/example/candidates/batch_summary.json \
    --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt --jobs 4

処理本体は avs/analysis.py（解析）と avs/prompts.py（プロンプト組み立て）にある。
"""

from __future__ import annotations

import argparse
import sys

from avs.analysis import (
    DEFAULT_MAX_TILES_PER_CALL,
    DEFAULT_MODEL,
    AnalyzeOptions,
    run_analysis,
)
from avs.common import configure_utf8_stdout
from avs.prompts import DEFAULT_OBJECTIVE, OBJECTIVE_PLACEHOLDER


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="manifest.json の全タイルをまとめて aitool recognize-image に渡します。複数 manifest 可。"
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
    parser.add_argument("-p", "--prompt", required=True, help="プロンプトテキストファイルのパス")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="解析結果の出力パス（manifest が1件のときのみ有効）。省略時は manifest 横に自動命名",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="複数 manifest の出力をまとめるディレクトリ。各 manifest を <名前>_analysis.* で出力",
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
        "--expect-json",
        action="store_true",
        help="出力をJSONとして検証し、失敗時は1回だけリトライする。整形JSONを .json に保存",
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
        help="複数 manifest を並列解析するワーカー数（既定: 1）",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"使用モデル（既定: {DEFAULT_MODEL}）",
    )
    parser.add_argument(
        "--aitool",
        default=None,
        help="aitool コマンドのパス。省略時は PATH から検索",
    )
    parser.add_argument("--api-key", default=None, help="OpenRouter APIキー（省略時は環境変数）")
    parser.add_argument("--dry-run", action="store_true", help="コマンドを表示するだけで実行しない")
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
