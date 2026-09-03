#!/usr/bin/env python
"""
動画をフレームタイル画像にまとめるCLI。単一範囲とconfig（複数範囲一括）の2モードがある。

単一範囲:
  python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py \
    --video video.mp4 --start 36 --end 46 --fps 5 --pad 2 \
    --output output/agentic_tiles/video_36_46_fps5.jpg

複数範囲（config）:
  python .agents/skills/agentic-video-analysis-skill/scripts/tile_video_frames.py \
    --config .agents/skills/agentic-video-analysis-skill/examples/ranges.example.json \
    --merge-overlaps

処理本体は avs/tiling.py（抽出・描画・manifest）と avs/ranges.py（config）にある。
"""

from __future__ import annotations

import argparse
import sys

from avs.common import configure_utf8_stdout
from avs.ranges import run_config_mode
from avs.tiling import (
    DEFAULT_EXTRACT_WIDTH,
    DEFAULT_FRAMES_PER_TILE,
    DEFAULT_QUALITY,
    DEFAULT_TILE_HEIGHT,
    DEFAULT_TILE_WIDTH,
    DEFAULT_ZOOM_QUALITY,
    TileOptions,
    parse_timestamps,
    tile_one_range,
    tile_zoom,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="動画を指定fpsでフレーム抽出し、ラベル付きフレームタイル画像を生成します。"
    )
    parser.add_argument("-v", "--video", default=None, help="入力動画パス（単一範囲モードで必須）")
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="複数範囲を一括処理する範囲定義JSONのパス。指定時は --start/--end 等を無視する",
    )
    parser.add_argument("--start", type=float, default=0.0, help="開始秒。省略時は0秒")
    parser.add_argument("--end", type=float, default=None, help="終了秒。省略時は動画末尾")
    parser.add_argument("--pad", type=float, default=0.0, help="開始・終了の前後に足す秒数")
    parser.add_argument("--fps", type=float, default=1.0, help="1秒あたりの抽出枚数")
    parser.add_argument(
        "--timestamps",
        default=None,
        help="ズームモード: 抽出する時刻をカンマ区切りで指定（例 38.5,40.2）。--start/--end/--fps と排他",
    )
    parser.add_argument("-o", "--output", default=None, help="出力画像パス、または複数タイル時のベースパス")
    parser.add_argument(
        "--metadata-output",
        default=None,
        help="メタデータJSONの出力パス。省略時は出力先に manifest.json",
    )
    parser.add_argument(
        "-f",
        "--frames-per-tile",
        type=int,
        default=DEFAULT_FRAMES_PER_TILE,
        help=f"1タイルあたりの最大フレーム数（既定: {DEFAULT_FRAMES_PER_TILE}）",
    )
    parser.add_argument("--cols", type=int, default=None, help="グリッド列数。省略時は自動")
    parser.add_argument(
        "--cell-width",
        type=int,
        default=None,
        help="各セル画像の幅px。省略時はタイルサイズから自動計算",
    )
    parser.add_argument("--tile-width", type=int, default=DEFAULT_TILE_WIDTH, help="タイル画像の目標幅px")
    parser.add_argument("--tile-height", type=int, default=DEFAULT_TILE_HEIGHT, help="タイル画像の目標高さpx")
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help=f"ffmpeg抽出時のリサイズ幅px（既定 {DEFAULT_EXTRACT_WIDTH}）。zoomモードでは未指定でフル解像度",
    )
    parser.add_argument("--label-height", type=int, default=28, help="各セル下部ラベルの高さpx")
    parser.add_argument("--gap", type=int, default=2, help="セル間の隙間px")
    parser.add_argument(
        "--quality",
        type=int,
        default=None,
        help=f"出力JPEG品質 1-95（タイル既定 {DEFAULT_QUALITY} / zoom既定 {DEFAULT_ZOOM_QUALITY}）",
    )
    parser.add_argument(
        "--ffmpeg-quality",
        type=int,
        default=5,
        help="一時フレーム抽出時のJPEG品質。小さいほど高品質",
    )
    parser.add_argument(
        "--keep-frames",
        action="store_true",
        help="一時抽出フレームを削除しない",
    )
    parser.add_argument(
        "--quiet-warnings",
        action="store_true",
        help="フレーム数に関する警告を抑制する",
    )
    parser.add_argument(
        "--merge-overlaps",
        action="store_true",
        help="config モードで重なる範囲をマージしてからタイル化する",
    )
    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=0.5,
        help="範囲マージの重なり率しきい値（既定: 0.5）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="config モードで処理予定の範囲を表示するだけで実行しない",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    options = TileOptions.from_namespace(parse_args(argv))
    if options.config:
        return run_config_mode(options)
    if options.timestamps:
        if not options.video:
            raise RuntimeError("--timestamps には --video が必要です")
        tile_zoom(options, parse_timestamps(options.timestamps))
        return 0
    if not options.video:
        raise RuntimeError("--video または --config を指定してください")
    tile_one_range(options)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
