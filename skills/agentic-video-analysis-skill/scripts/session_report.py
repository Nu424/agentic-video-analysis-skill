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
from typing import Any

from avs.common import configure_utf8_stdout


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


def render_text(report: dict[str, Any]) -> str:
    usage = report["usage"]
    next_actions = report["next_actions"]
    estimate = report["estimate"]

    lines = [f"# セッションレポート: {report['session']}", ""]

    lines.append("## 呼び出しと使用量")
    lines.append(f"- 呼び出し回数: {usage['calls']}")
    tokens = usage["tokens"]
    lines.append(
        f"- トークン: input={tokens['input_tokens']} output={tokens['output_tokens']}"
        f" reasoning={tokens['reasoning_tokens']} total={tokens['total_tokens']}"
    )
    note = "（概算を含む）" if usage["cost_has_estimate"] else "（実測）"
    lines.append(
        f"- コスト USD: 実測={usage['cost_usd_actual']:.4f}"
        f" / 概算={usage['cost_usd_estimated']:.4f}"
        f" / 合計={usage['cost_usd_total']:.4f} {note}"
    )
    if usage["unknown_cost_calls"]:
        lines.append(f"- コスト不明の呼び出し: {usage['unknown_cost_calls']} 件（単価表に無いモデル）")
    lines.append(f"- 所要時間(latency)合計: {usage['latency_sec']:.1f}s")

    if usage["by_step"]:
        lines.append("")
        lines.append("## ステップ別")
        for step, bucket in sorted(usage["by_step"].items()):
            lines.append(
                f"- {step}: 呼び出し{bucket['calls']}件"
                f" / total_tokens={bucket['total_tokens']}"
                f" / cost_usd={bucket['cost_usd']:.4f}"
            )

    lines.append("")
    lines.append("## 次アクション候補")
    lines.append(f"- confidence=low: {next_actions['low_confidence_count']} 件")
    zoom_targets = next_actions["zoom_targets"]
    lines.append(f"- zoom_targets: {len(zoom_targets)} 件")
    for item in zoom_targets[:20]:
        lines.append(f"  - t={item['timestamp_sec']}s ({item['file']})")
    if len(zoom_targets) > 20:
        lines.append(f"  - ...他 {len(zoom_targets) - 20} 件")
    lines.append(f"- hypothesis_verdict=rejected: {len(next_actions['hypothesis_rejected'])} 件")
    for path in next_actions["hypothesis_rejected"]:
        lines.append(f"  - {path}")
    lines.append(f"- 失敗した範囲: {len(next_actions['failed_ranges'])} 件")
    for item in next_actions["failed_ranges"]:
        lines.append(f"  - {item.get('label')}: {item.get('error')} ({item['file']})")
    if next_actions["validated_flags_count"]:
        lines.append(f"- validated flags 合計: {next_actions['validated_flags_count']} 件")

    if estimate is not None:
        lines.append("")
        lines.append("## 事前見積もり（--estimate）")
        lines.append(f"- 範囲数: {estimate['n_ranges']}")
        lines.append(f"- 推定タイル数: {estimate['n_tiles_estimate']}")
        measured = "実測平均" if estimate["tokens_are_measured"] else "仮定値"
        lines.append(f"- 1タイルあたりトークン({measured}): {estimate['avg_tokens_per_tile']}")
        lines.append(
            f"- 概算コスト USD: {estimate['estimated_usd']:.4f}"
            f"（{estimate['model_used_for_pricing']} の単価表より概算）"
        )

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    args = parse_args(argv)

    from avs.session import build_report  # noqa: PLC0415 - import 時間短縮

    report = build_report(args.session, do_estimate=args.estimate)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
