#!/usr/bin/env python
"""複数実行の和集合網羅率を測る。

**見逃しは事象ごとにランダムに起きやすい**（`eval/README.md` §1・§8）。同一条件で
複数回実行し、どれか 1 回でも検出できた事象の割合（和集合網羅率）を見ると、単発の
網羅率より実態に近い上限が分かる。

出す指標:

- 各実行単独の検出集合と網羅率
- 単独網羅率の平均
- 2 実行の全組み合わせの和集合網羅率の平均・最小・最大
- 渡した実行のいずれも検出できなかった事象の一覧

使い方:

  python eval/union_recall.py --gt eval/fixtures/<name>/ground_truth.json \
      run1/timeline.json run2/timeline.json run3/timeline.json

  python eval/union_recall.py --gt eval/fixtures/<name>/ground_truth.json \
      run1/timeline.json run2/timeline.json --json
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

# score.py と同じディレクトリ（eval/）にある前提。`python eval/union_recall.py` の
# 通常起動ではスクリプト自身のディレクトリが sys.path[0] になるため import できる。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from score import (  # noqa: E402
    GroundTruthError,
    ScoreError,
    configure_utf8_stdout,
    load_entries,
    load_ground_truth,
    overlaps,
)


def detected_ids(entries: list[tuple[float, float, str]], gt: dict[str, Any]) -> set[str]:
    """1 実行分のエントリ列から、検出できた正解 event の id 集合を返す。"""
    ids: set[str] = set()
    for ev in gt["events"]:
        w0, w1 = ev["match"]["window"]
        patterns = ev["match"]["any"]
        if any(
            overlaps(s, e, w0, w1) and any(re.search(pat, t) for pat in patterns)
            for s, e, t in entries
        ):
            ids.add(ev["id"])
    return ids


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="複数実行の和集合網羅率を測る（同一条件で複数回実行した結果を比較する）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  python eval/union_recall.py --gt eval/fixtures/<name>/ground_truth.json"
            " run1/timeline.json run2/timeline.json run3/timeline.json\n"
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="同一条件で実行した出力ファイル（timeline.json 等）。2 個以上を推奨",
    )
    parser.add_argument("--gt", required=True, help="正解データ（ground_truth.json）へのパス")
    parser.add_argument("--json", action="store_true", help="結果を JSON で標準出力する")
    return parser


def compute(paths: list[str], gt: dict[str, Any]) -> dict[str, Any]:
    per_run = [(p, detected_ids(load_entries(p), gt)) for p in paths]

    n_events = len(gt["events"])

    def pct(ids: set[str]) -> float:
        return round(100 * len(ids) / n_events, 1) if n_events else 0.0

    solo = [
        {"file": p, "detected": sorted(ids), "recall_pct": pct(ids)} for p, ids in per_run
    ]
    solo_pcts = [row["recall_pct"] for row in solo]

    pair_pcts: list[float] = []
    for (_, ia), (_, ib) in itertools.combinations(per_run, 2):
        pair_pcts.append(pct(ia | ib))

    all_detected: set[str] = set()
    for _, ids in per_run:
        all_detected |= ids
    never_detected = [
        {"id": ev["id"], "name": ev["name"], "start": ev["start"], "end": ev["end"]}
        for ev in gt["events"]
        if ev["id"] not in all_detected
    ]

    return {
        "n_runs": len(per_run),
        "n_events": n_events,
        "solo": solo,
        "solo_recall_pct_mean": round(statistics.mean(solo_pcts), 1) if solo_pcts else 0.0,
        "pair_union_recall_pct_mean": round(statistics.mean(pair_pcts), 1) if pair_pcts else None,
        "pair_union_recall_pct_min": round(min(pair_pcts), 1) if pair_pcts else None,
        "pair_union_recall_pct_max": round(max(pair_pcts), 1) if pair_pcts else None,
        "never_detected": never_detected,
    }


def _print_result(result: dict[str, Any]) -> None:
    print(f"実行数: {result['n_runs']}  正解事象数: {result['n_events']}")
    print()
    for row in result["solo"]:
        print(f"  {row['file']}: {row['recall_pct']}%  検出={', '.join(row['detected']) or 'なし'}")
    print()
    print(f"単独網羅率の平均: {result['solo_recall_pct_mean']}%")
    if result["pair_union_recall_pct_mean"] is not None:
        print(
            "2実行の和集合網羅率: "
            f"平均 {result['pair_union_recall_pct_mean']}%"
            f"  最小 {result['pair_union_recall_pct_min']}%"
            f"  最大 {result['pair_union_recall_pct_max']}%"
        )
    else:
        print("2実行の和集合網羅率: 実行数が2未満のため計算できません")
    print()
    if result["never_detected"]:
        print("全実行のいずれも検出できなかった事象:")
        for ev in result["never_detected"]:
            print(f"  {ev['id']} {ev['start']}-{ev['end']} {ev['name']}")
    else:
        print("全実行のいずれも検出できなかった事象: なし")


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdout()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        gt = load_ground_truth(args.gt)
        result = compute(args.paths, gt)
    except (GroundTruthError, ScoreError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
