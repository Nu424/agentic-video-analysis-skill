#!/usr/bin/env python
"""overview 解析結果（candidates[]）から、全区間カバーの ranges.json を作るCLI。

  python skills/agentic-video-analysis-skill/scripts/plan_ranges.py \
    --overview output/agentic_sessions/example/overview/overview_analysis.json \
    --video video.mp4 \
    --output output/agentic_sessions/example/ranges/ranges.json \
    --coverage full --detail-fps 5

`--coverage auto`（既定）は動画長が `--full-coverage-max-sec`（既定600秒）以下なら full、
超えるなら priority を自動選択する。

出力の ranges.json は `tile_video_frames.py --config` と `analyze.py --ranges` の
両方が読める config 形式（`video` / `output_dir` / `defaults` / `ranges` / `summary_output`）。

処理本体は avs/ranges.py の plan_full_coverage()。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from avs.common import configure_utf8_stdout, probe_duration_sec, read_json, write_json
from avs.ranges import plan_full_coverage

DEFAULT_FULL_COVERAGE_MAX_SEC = 600.0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="overview 解析結果（candidates[]）から、全区間カバーの ranges.json を作ります。"
    )
    parser.add_argument("--overview", required=True, help="overview 解析結果のJSON（candidates キーを持つ）")
    parser.add_argument("--video", required=True, help="動画パス（ffprobe で長さを取得する）")
    parser.add_argument("--output", required=True, help="出力する ranges.json のパス")
    parser.add_argument(
        "--coverage",
        choices=["auto", "full", "priority", "high-only"],
        default="auto",
        help=(
            "範囲網羅の方式（既定: auto。動画長が --full-coverage-max-sec 以下なら full、"
            "超えるなら priority を自動選択）"
        ),
    )
    parser.add_argument("--detail-fps", type=float, default=5.0, help="high/medium 範囲の fps（既定: 5）")
    parser.add_argument(
        "--low-fps", type=float, default=1.0, help="priority モードで low 範囲に使う fps（既定: 1）"
    )
    parser.add_argument("--max-range-sec", type=float, default=8.0, help="1範囲の最大秒数（既定: 8）")
    parser.add_argument(
        "--min-range-sec", type=float, default=2.0, help="1範囲の最小秒数。未満は隣とマージする（既定: 2）"
    )
    parser.add_argument(
        "--min-gap-sec",
        type=float,
        default=1.0,
        help="この秒数以上の隙間があれば gap 範囲で埋める（既定: 1）",
    )
    parser.add_argument("--pad", type=float, default=1.0, help="defaults.pad に設定する値（既定: 1.0）")
    parser.add_argument(
        "--frames-per-tile", type=int, default=12, help="defaults.frames_per_tile に設定する値（既定: 12）"
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="ranges.json 内の output_dir。省略時は --output の親ディレクトリ",
    )
    parser.add_argument(
        "--full-coverage-max-sec",
        type=float,
        default=DEFAULT_FULL_COVERAGE_MAX_SEC,
        help=(
            "--coverage auto のとき、この秒数以下なら full、超えたら priority を選ぶ"
            f"（既定: {DEFAULT_FULL_COVERAGE_MAX_SEC:g}）"
        ),
    )
    return parser.parse_args(argv)


def resolve_coverage(coverage: str, duration_sec: float, full_coverage_max_sec: float) -> str:
    if coverage != "auto":
        return coverage
    return "full" if duration_sec <= full_coverage_max_sec else "priority"


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    args = parse_args(argv)

    overview_path = Path(args.overview).expanduser().resolve()
    overview = read_json(overview_path)
    candidates = overview.get("candidates") or []

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        raise RuntimeError(f"動画ファイルが見つかりません: {video_path}")
    duration_sec = probe_duration_sec(video_path)

    coverage = resolve_coverage(args.coverage, duration_sec, args.full_coverage_max_sec)
    print(f"動画長: {duration_sec:.1f}s / coverage: {coverage} / 候補数: {len(candidates)}")

    plan = plan_full_coverage(
        candidates,
        duration_sec,
        max_range_sec=args.max_range_sec,
        min_range_sec=args.min_range_sec,
        min_gap_sec=args.min_gap_sec,
        coverage=coverage,
        detail_fps=args.detail_fps,
        low_fps=args.low_fps,
        pad=args.pad,
    )

    output_path = Path(args.output).expanduser().resolve()
    output_dir = (
        Path(args.output_dir).expanduser().resolve() if args.output_dir else output_path.parent
    )
    plan["video"] = str(video_path)
    plan["output_dir"] = str(output_dir)
    plan["summary_output"] = str(output_dir / "batch_summary.json")
    plan["defaults"]["frames_per_tile"] = args.frames_per_tile

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, plan)

    dropped = len(plan["plan"]["dropped"])
    print(
        f"範囲数: {plan['plan']['n_ranges']}"
        + (f" / 除外(dropped, high-only): {dropped}" if dropped else "")
    )
    print(f"ranges: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
