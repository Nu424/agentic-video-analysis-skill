#!/usr/bin/env python
"""detail の解析結果に後段バリデーションのフラグを付けるCLI。

**出来事は削除しない。** 各 event に `flags` と `confidence_adjusted` を足した
`<name>_validated.json` を書き、`--report` を付ければフラグ集計
（`validation_report.json`）も書く。採用・不採用の判断はエージェントが行う。

一括（タイル化の batch_summary.json から解析結果と manifest を対応付ける）:
  python skills/agentic-video-analysis-skill/scripts/validate_analysis.py \
    --summary output/agentic_sessions/example/ranges/batch_summary.json \
    --domain notes/domain.json --report

個別指定（manifest は同名ディレクトリから自動で探す。無ければセルラベル系ルールをスキップ）:
  python skills/agentic-video-analysis-skill/scripts/validate_analysis.py \
    --analysis output/agentic_sessions/example/ranges/cand_a_analysis.json

処理本体は avs/validate.py にある。
"""

from __future__ import annotations

import argparse
import sys

from avs.common import configure_utf8_stdout
from avs.validate import DEFAULT_MAX_EVENT_SEC, REPORT_FILENAME, ValidateOptions, run_validation


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "detail の解析結果を検証し、各 event に flags と confidence_adjusted を付けた "
            "<name>_validated.json を書きます。出来事は削除しません。"
        )
    )
    parser.add_argument(
        "-s",
        "--summary",
        default=None,
        help=(
            "タイル化の batch_summary.json。results[].manifest_path から "
            "解析結果と manifest を対応付ける"
        ),
    )
    parser.add_argument(
        "-a",
        "--analysis",
        nargs="+",
        default=None,
        help="解析結果 <name>_analysis.json のパス（複数指定可）",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="ドメイン定義 domain.json。negatives[].pattern / window を negative_match に使う",
    )
    parser.add_argument(
        "--max-event-sec",
        type=float,
        default=DEFAULT_MAX_EVENT_SEC,
        help=f"この秒数より長い出来事に duration_outlier を付ける（既定: {DEFAULT_MAX_EVENT_SEC:g}）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="<name>_validated.json の出力先ディレクトリ。省略時は解析結果と同じ場所",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=f"フラグ集計を {REPORT_FILENAME} に書く（既定の場所は <session>/merge/）",
    )
    parser.add_argument(
        "--report-output",
        default=None,
        help=f"{REPORT_FILENAME} の出力パスを明示する（指定すると --report も有効になる）",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="セッションディレクトリ。レポートの既定の出力先に使う（省略時は解析結果から上方向に探す）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    return run_validation(ValidateOptions.from_namespace(parse_args(argv)))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
