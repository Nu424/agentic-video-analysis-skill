#!/usr/bin/env python
"""セッションのコスト集計と次アクション候補を表示するCLI。

`usage.jsonl` を集計し、呼び出し回数・トークン内訳・USD（実測/概算を分けて表示）・
所要時間・ステップ別集計を出す。あわせてセッション内の `*_analysis.json` /
`*_validated.json` / `batch_summary.json` / `analysis_summary.json` を走査し、
次に確認すべき候補（低確信・ズーム対象・仮説反証・失敗した範囲）を挙げる。

  python skills/agentic-video-analysis-skill/scripts/session_report.py \
    --session output/agentic_sessions/example

`--estimate` を付けると、`ranges/ranges.json` があれば解析実行前の概算コストも出す
（範囲数 × タイル数の見積もり × 1タイルあたり平均トークン × 単価表）。

処理本体は avs/session.py にある。
"""

from __future__ import annotations

import argparse
import json
import sys

from avs.common import configure_utf8_stdout
from avs.session import build_report, render_report_text


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="セッションのコスト集計と次アクション候補を表示します。"
    )
    parser.add_argument("--session", required=True, help="セッションディレクトリ")
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="ranges/ranges.json があれば、解析実行前の概算コストも出す",
    )
    parser.add_argument("--json", action="store_true", help="結果をJSONで出力する（既定は人が読むテキスト）")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    args = parse_args(argv)

    report = build_report(args.session, do_estimate=args.estimate)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report_text(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
