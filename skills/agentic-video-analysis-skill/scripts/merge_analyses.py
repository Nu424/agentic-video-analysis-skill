#!/usr/bin/env python
"""範囲ごとの解析結果を統合して timeline.json / final.md を作るCLI。

セッション全体を統合（`_validated.json` を優先し、無ければ `_analysis.json`）:
  python skills/agentic-video-analysis-skill/scripts/merge_analyses.py \
    --session output/agentic_sessions/example --objective "動画の見どころ候補の抽出" \
    --domain notes/domain.json --final-md

LLM 統合を省く（機械統合だけ。API を呼ばない）:
  python skills/agentic-video-analysis-skill/scripts/merge_analyses.py \
    --session output/agentic_sessions/example --no-llm --final-md

複数実行の和集合（各項目に runs が付く）:
  python skills/agentic-video-analysis-skill/scripts/merge_analyses.py \
    --union output/agentic_sessions/run1/merge/timeline.json \
            output/agentic_sessions/run2/merge/timeline.json \
    --output output/agentic_sessions/run1/merge/timeline_union.json --final-md

音声の取り込み（映像と重ならない項目に audio_unconfirmed が付く）:
  python skills/agentic-video-analysis-skill/scripts/merge_analyses.py \
    --session output/agentic_sessions/example \
    --audio output/agentic_sessions/example/audio/audio_analysis.json --final-md

出力（既定は <session>/merge/ 配下）:
  timeline_mechanical.json  機械統合の結果（LLM統合前の監査用。常に書く）
  timeline.json             最終タイムライン（--union のときは timeline_union.json）
  validation_report.json    LLM統合前後の件数・時間差分を merge_diff として追記
  final.md                  --final-md のとき（既定は <session>/final.md）

処理本体は avs/merge.py にある。
"""

from __future__ import annotations

import argparse
import sys

from avs.backends import BACKEND_NAMES
from avs.common import configure_utf8_stdout
from avs.merge import (
    DEFAULT_BACKEND,
    DEFAULT_LLM_CHUNK,
    DEFAULT_OVERLAP_THRESHOLD,
    DEFAULT_TITLE_SIMILARITY,
    MergeOptions,
    run_merge,
)
from avs.prompts import DEFAULT_OBJECTIVE


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "範囲ごとの解析結果を機械統合し、LLM統合を経て timeline.json と final.md を作ります。"
            "複数実行の和集合（--union）と音声の取り込み（--audio）にも対応します。"
        )
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "セッションディレクトリ。配下の <label>_validated.json（無ければ <label>_analysis.json）"
            "を集める。overview / zooms / refinements / audio は対象外"
        ),
    )
    parser.add_argument(
        "--inputs",
        nargs="+",
        default=None,
        help="統合する解析結果を明示指定する（--session の代わり）",
    )
    parser.add_argument(
        "--union",
        nargs="+",
        default=None,
        help="複数実行の timeline.json を機械的に合成する（各項目に runs が付く）",
    )
    parser.add_argument(
        "--audio",
        default=None,
        help="音声解析 audio_analysis.json。segments[] を source=audio の項目として timeline に足す",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="timeline の出力パス（既定: <session>/merge/timeline.json、--union は timeline_union.json）",
    )
    parser.add_argument(
        "--final-md",
        action="store_true",
        help="timeline から final.md を生成する（既定の出力先は <session>/final.md）",
    )
    parser.add_argument(
        "--output-md",
        default=None,
        help="final.md の出力パスを明示する（指定すると --final-md も有効になる）",
    )
    parser.add_argument(
        "--objective",
        default=None,
        help=f"解析の目的（テキスト or ファイルパス）。LLM統合のプロンプトに入る。既定: {DEFAULT_OBJECTIVE}",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="ドメイン定義 domain.json。importance_rubric が LLM統合の重要度基準になる",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="LLM統合を省き、機械統合の結果をそのまま timeline.json にする（API を呼ばない）",
    )
    parser.add_argument(
        "--llm-chunk",
        type=int,
        default=DEFAULT_LLM_CHUNK,
        help=f"1回のLLM統合に渡す項目数の上限（既定: {DEFAULT_LLM_CHUNK}）。超過分は時間順に分割",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="LLM統合に使うプロンプト。省略時はスキル同梱の prompts/merge.txt",
    )
    parser.add_argument(
        "--backend",
        choices=list(BACKEND_NAMES),
        default=DEFAULT_BACKEND,
        help=f"LLM統合に使うバックエンド（既定: {DEFAULT_BACKEND}）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="使用モデル。省略時はバックエンドの既定",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="APIキー。省略時は 環境変数 -> ./.env -> ~/.env.global の順に探す",
    )
    parser.add_argument(
        "--strict-json",
        action="store_true",
        help="LLM統合で構造化出力（response_format）を要求する",
    )
    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=DEFAULT_OVERLAP_THRESHOLD,
        help=f"同一とみなす時間の重なり率（短い方基準。既定: {DEFAULT_OVERLAP_THRESHOLD}）",
    )
    parser.add_argument(
        "--title-similarity",
        type=float,
        default=DEFAULT_TITLE_SIMILARITY,
        help=f"同一とみなすタイトル類似度（既定: {DEFAULT_TITLE_SIMILARITY}）",
    )
    parser.add_argument(
        "--report-output",
        default=None,
        help="merge_diff を書く validation_report.json のパス（既定: timeline と同じディレクトリ）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    return run_merge(MergeOptions.from_namespace(parse_args(argv)))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
