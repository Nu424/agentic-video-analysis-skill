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
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

DEFAULT_FRAMES_PER_TILE = 12
DEFAULT_TILE_WIDTH = 1920
DEFAULT_TILE_HEIGHT = 1080
DEFAULT_EXTRACT_WIDTH = 640
WARN_FRAMES_PER_TILE = 16


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
        default=DEFAULT_EXTRACT_WIDTH,
        help="ffmpeg抽出時のリサイズ幅px。高さはアスペクト比維持",
    )
    parser.add_argument("--label-height", type=int, default=28, help="各セル下部ラベルの高さpx")
    parser.add_argument("--gap", type=int, default=2, help="セル間の隙間px")
    parser.add_argument("--quality", type=int, default=90, help="出力JPEG品質 1-95")
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


def run_command(args: list[str], label: str) -> str:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{args[0]} が見つかりません。ffmpeg/ffprobeをインストールしてください") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"{label} に失敗しました (exit {result.returncode}): {stderr}")

    return result.stdout.strip()


def probe_duration_sec(video_path: Path) -> float:
    output = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        "ffprobe",
    )
    try:
        duration = float(output)
    except ValueError as exc:
        raise RuntimeError(f"動画の長さを取得できませんでした: {video_path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"動画の長さが不正です: {duration}")
    return duration


def probe_video_size(video_path: Path) -> tuple[int, int]:
    output = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(video_path),
        ],
        "ffprobe (size)",
    )
    try:
        width, height = [int(value) for value in output.split("x")]
    except ValueError as exc:
        raise RuntimeError(f"動画サイズを取得できませんでした: {output}") from exc
    return width, height


def format_float_for_path(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text.replace(".", "_")


def default_output_path(video_path: Path, start_sec: float, end_sec: float, fps: float) -> Path:
    stem = video_path.stem
    start_label = format_float_for_path(start_sec)
    end_label = format_float_for_path(end_sec)
    fps_label = format_float_for_path(fps)
    return Path("output") / "agentic_tiles" / f"{stem}_{start_label}_{end_label}_fps{fps_label}.jpg"


def apply_pad(start_sec: float, end_sec: float, pad_sec: float, duration_sec: float) -> tuple[float, float]:
    padded_start = max(0.0, start_sec - pad_sec)
    padded_end = min(duration_sec, end_sec + pad_sec)
    if padded_end <= padded_start:
        raise RuntimeError("パディング後の範囲が空です。start/end/pad を確認してください")
    return padded_start, padded_end


def resolve_output_layout(output_path: Path, tile_count: int) -> dict[str, Any]:
    if tile_count == 1:
        return {
            "mode": "single",
            "tile_paths": [output_path],
            "manifest_path": output_path.with_suffix(".json"),
        }

    if output_path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        output_dir = output_path.parent / output_path.stem
    else:
        output_dir = output_path

    tile_paths = [output_dir / f"tile_{index:03d}.jpg" for index in range(tile_count)]
    return {
        "mode": "multi",
        "output_dir": output_dir,
        "tile_paths": tile_paths,
        "manifest_path": output_dir / "manifest.json",
    }


def validate_options(options: argparse.Namespace) -> dict[str, Any]:
    video_path = Path(options.video).expanduser().resolve()
    if not video_path.exists():
        raise RuntimeError(f"動画ファイルが見つかりません: {video_path}")
    if options.fps <= 0:
        raise RuntimeError("--fps は 0 より大きい値を指定してください")
    if options.start < 0:
        raise RuntimeError("--start は 0 以上を指定してください")
    if options.pad < 0:
        raise RuntimeError("--pad は 0 以上を指定してください")
    if options.frames_per_tile < 1:
        raise RuntimeError("--frames-per-tile は 1 以上を指定してください")
    if options.cols is not None and options.cols < 1:
        raise RuntimeError("--cols は 1 以上を指定してください")
    if options.cell_width is not None and options.cell_width < 1:
        raise RuntimeError("--cell-width は 1 以上を指定してください")
    if options.tile_width < 1 or options.tile_height < 1:
        raise RuntimeError("--tile-width / --tile-height は 1 以上を指定してください")
    if options.width < 1:
        raise RuntimeError("--width は 1 以上を指定してください")
    if options.label_height < 1:
        raise RuntimeError("--label-height は 1 以上を指定してください")
    if options.gap < 0:
        raise RuntimeError("--gap は 0 以上を指定してください")
    if not 1 <= options.quality <= 95:
        raise RuntimeError("--quality は 1 から 95 の範囲で指定してください")

    duration_sec = probe_duration_sec(video_path)
    source_width, source_height = probe_video_size(video_path)
    requested_start = options.start
    requested_end = duration_sec if options.end is None else options.end

    if requested_end <= requested_start:
        raise RuntimeError("--end は --start より大きい値を指定してください")
    if requested_start >= duration_sec:
        raise RuntimeError(
            f"--start が動画の長さを超えています: {requested_start:.3f}s >= {duration_sec:.3f}s"
        )

    start_sec, end_sec = apply_pad(
        requested_start,
        min(requested_end, duration_sec),
        options.pad,
        duration_sec,
    )

    output_path = (
        Path(options.output).expanduser().resolve()
        if options.output
        else default_output_path(video_path, start_sec, end_sec, options.fps).resolve()
    )
    metadata_output = (
        Path(options.metadata_output).expanduser().resolve()
        if options.metadata_output
        else None
    )

    return {
        "video_path": video_path,
        "duration_sec": duration_sec,
        "source_width": source_width,
        "source_height": source_height,
        "requested_start_sec": requested_start,
        "requested_end_sec": min(requested_end, duration_sec),
        "start_sec": start_sec,
        "end_sec": end_sec,
        "output_path": output_path,
        "metadata_output": metadata_output,
    }


def extract_frames(
    video_path: Path,
    frames_dir: Path,
    start_sec: float,
    end_sec: float,
    fps: float,
    extract_width: int,
    ffmpeg_quality: int,
) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = frames_dir / "frame_%06d.jpg"
    duration = end_sec - start_sec
    vf = f"fps={fps},scale={extract_width}:-2"

    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_sec:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-start_number",
            "0",
            "-q:v",
            str(ffmpeg_quality),
            str(output_pattern),
        ],
        "ffmpeg",
    )

    frames = sorted(frames_dir.glob("frame_*.jpg"))
    if not frames:
        raise RuntimeError("抽出されたフレームが0枚です。範囲またはfpsを確認してください")
    return frames


def chunk_frames(frames: list[Path], frames_per_tile: int) -> list[list[Path]]:
    return [frames[index : index + frames_per_tile] for index in range(0, len(frames), frames_per_tile)]


def auto_cols(frames_per_tile: int) -> int:
    return max(1, math.ceil(math.sqrt(frames_per_tile)))


def compute_grid(frames_in_tile: int, cols_option: int | None, frames_per_tile: int) -> tuple[int, int]:
    cols = cols_option if cols_option is not None else auto_cols(min(frames_in_tile, frames_per_tile))
    rows = math.ceil(frames_in_tile / cols)
    return cols, rows


def compute_cell_width(
    cols: int,
    rows: int,
    gap: int,
    label_height: int,
    aspect: float,
    target_tile_width: int,
    target_tile_height: int,
) -> int:
    from_width = (target_tile_width - (cols - 1) * gap) / cols
    cell_total_height = (target_tile_height - (rows - 1) * gap) / rows
    from_height = max(1.0, cell_total_height - label_height) / aspect
    return max(1, math.floor(min(from_width, from_height)))


def warn_if_needed(frame_count: int, frames_per_tile: int, quiet: bool) -> None:
    if quiet:
        return
    if frame_count <= frames_per_tile:
        return
    if frames_per_tile > WARN_FRAMES_PER_TILE:
        warnings.warn(
            f"--frames-per-tile={frames_per_tile} は大きめです。"
            f"視認性のため {DEFAULT_FRAMES_PER_TILE} 前後を推奨します。",
            stacklevel=2,
        )
    warnings.warn(
        f"抽出フレーム数 {frame_count} 枚を {math.ceil(frame_count / frames_per_tile)} タイルに分割します。",
        stacklevel=2,
    )


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_tile(
    frames_chunk: list[Path],
    range_start_sec: float,
    fps: float,
    frame_offset: int,
    cell_width: int | None,
    label_height: int,
    gap: int,
    cols_option: int | None,
    frames_per_tile: int,
    tile_width: int,
    tile_height: int,
) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(frames_chunk[0]) as sample:
        source_width, source_height = sample.size
        aspect = source_height / source_width

    cols, rows = compute_grid(len(frames_chunk), cols_option, frames_per_tile)
    resolved_cell_width = cell_width or compute_cell_width(
        cols,
        rows,
        gap,
        label_height,
        aspect,
        tile_width,
        tile_height,
    )
    cell_height = max(1, round(resolved_cell_width * aspect))
    tile_image_width = cols * resolved_cell_width + (cols - 1) * gap
    tile_image_height = rows * (cell_height + label_height) + (rows - 1) * gap

    tile = Image.new("RGB", (tile_image_width, tile_image_height), "#1a1a1a")
    font = load_font(max(12, min(24, round(label_height * 0.55))))
    cells: list[dict[str, Any]] = []

    for local_index, frame_path in enumerate(frames_chunk):
        frame_index = frame_offset + local_index
        row = local_index // cols
        col = local_index % cols
        left = col * (resolved_cell_width + gap)
        top = row * (cell_height + label_height + gap)
        timestamp_sec = range_start_sec + frame_index / fps

        with Image.open(frame_path) as frame:
            frame_rgb = frame.convert("RGB").resize(
                (resolved_cell_width, cell_height),
                Image.Resampling.LANCZOS,
            )

        tile.paste(frame_rgb, (left, top))
        draw = ImageDraw.Draw(tile)
        label_top = top + cell_height
        draw.rectangle(
            (left, label_top, left + resolved_cell_width, label_top + label_height),
            fill=(0, 0, 0),
        )
        draw.text(
            (left + 6, label_top + max(1, round(label_height * 0.18))),
            f"F{frame_index} t={timestamp_sec:.1f}s",
            fill=(255, 255, 255),
            font=font,
        )
        cells.append(
            {
                "frame_index": frame_index,
                "timestamp_sec": round(timestamp_sec, 6),
                "filename": frame_path.name,
                "row": row,
                "col": col,
            }
        )

    metadata = {
        "grid": {"cols": cols, "rows": rows},
        "cell_width": resolved_cell_width,
        "cell_height": cell_height,
        "label_height": label_height,
        "gap": gap,
        "tile_width": tile_image_width,
        "tile_height": tile_image_height,
        "frame_count": len(frames_chunk),
        "cells": cells,
    }
    return tile, metadata


def build_single_metadata(
    context: dict[str, Any],
    options: argparse.Namespace,
    tile_entry: dict[str, Any],
    temp_frames_dir: Path | None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "approach": "agentic_video_frame_tile",
        "source": {
            "video_path": str(context["video_path"]),
            "duration_sec": context["duration_sec"],
            "width": context["source_width"],
            "height": context["source_height"],
        },
        "extraction": {
            "requested_start_sec": context["requested_start_sec"],
            "requested_end_sec": context["requested_end_sec"],
            "start_sec": context["start_sec"],
            "end_sec": context["end_sec"],
            "duration_sec": context["end_sec"] - context["start_sec"],
            "pad_sec": options.pad,
            "fps": options.fps,
            "extract_width": options.width,
            "kept_frames_dir": str(temp_frames_dir) if temp_frames_dir else None,
        },
        "tiling": {
            "frames_per_tile": options.frames_per_tile,
            "tile_count": 1,
            **{key: tile_entry[key] for key in tile_entry if key != "filename"},
        },
        "tiles": [tile_entry],
    }


def build_multi_metadata(
    context: dict[str, Any],
    options: argparse.Namespace,
    tile_entries: list[dict[str, Any]],
    temp_frames_dir: Path | None,
) -> dict[str, Any]:
    first = tile_entries[0]
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "approach": "agentic_video_frame_tiles",
        "source": {
            "video_path": str(context["video_path"]),
            "duration_sec": context["duration_sec"],
            "width": context["source_width"],
            "height": context["source_height"],
        },
        "extraction": {
            "requested_start_sec": context["requested_start_sec"],
            "requested_end_sec": context["requested_end_sec"],
            "start_sec": context["start_sec"],
            "end_sec": context["end_sec"],
            "duration_sec": context["end_sec"] - context["start_sec"],
            "pad_sec": options.pad,
            "fps": options.fps,
            "extract_width": options.width,
            "frame_count_total": sum(tile["frame_count"] for tile in tile_entries),
            "kept_frames_dir": str(temp_frames_dir) if temp_frames_dir else None,
        },
        "tiling": {
            "frames_per_tile": options.frames_per_tile,
            "tile_count": len(tile_entries),
            "cols": first["grid"]["cols"],
            "rows": first["grid"]["rows"],
            "cell_width": first["cell_width"],
            "cell_height": first["cell_height"],
            "label_height": options.label_height,
            "gap": options.gap,
            "tile_width": first["tile_width"],
            "tile_height": first["tile_height"],
        },
        "tiles": tile_entries,
    }


def tile_one_range(options: argparse.Namespace) -> dict[str, Any]:
    """単一範囲をタイル化し、manifest を書き出す。出力情報を返す。"""
    context = validate_options(options)
    output_path: Path = context["output_path"]
    metadata_output_override: Path | None = context["metadata_output"]

    temp_root = Path(tempfile.mkdtemp(prefix="agentic_video_frames_"))
    frames_dir = temp_root / "frames"
    kept_frames_dir: Path | None = temp_root if options.keep_frames else None

    try:
        print(f"入力動画: {context['video_path']}")
        print(
            f"範囲:     {context['start_sec']:.3f}s - {context['end_sec']:.3f}s"
            + (f" (pad={options.pad})" if options.pad else "")
        )
        print(f"fps:      {options.fps}")

        frames = extract_frames(
            context["video_path"],
            frames_dir,
            context["start_sec"],
            context["end_sec"],
            options.fps,
            options.width,
            options.ffmpeg_quality,
        )
        warn_if_needed(len(frames), options.frames_per_tile, options.quiet_warnings)

        chunks = chunk_frames(frames, options.frames_per_tile)
        layout = resolve_output_layout(output_path, len(chunks))
        manifest_path = metadata_output_override or layout["manifest_path"]

        tile_entries: list[dict[str, Any]] = []
        for tile_index, chunk in enumerate(chunks):
            tile_image, render_metadata = render_tile(
                chunk,
                context["start_sec"],
                options.fps,
                tile_index * options.frames_per_tile,
                options.cell_width,
                options.label_height,
                options.gap,
                options.cols,
                options.frames_per_tile,
                options.tile_width,
                options.tile_height,
            )
            tile_path = layout["tile_paths"][tile_index]
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            tile_image.save(tile_path, quality=options.quality)

            cells = render_metadata["cells"]
            tile_entries.append(
                {
                    "tile_index": tile_index,
                    "filename": tile_path.name,
                    "path": str(tile_path),
                    "start_frame": cells[0]["frame_index"],
                    "end_frame": cells[-1]["frame_index"],
                    "start_timestamp_sec": cells[0]["timestamp_sec"],
                    "end_timestamp_sec": cells[-1]["timestamp_sec"],
                    "frame_count": render_metadata["frame_count"],
                    "grid": render_metadata["grid"],
                    "cells": cells,
                    **{
                        key: render_metadata[key]
                        for key in (
                            "cell_width",
                            "cell_height",
                            "tile_width",
                            "tile_height",
                        )
                    },
                }
            )
            print(
                f"  タイル {tile_index}: frames {cells[0]['frame_index']}-{cells[-1]['frame_index']}"
                f" ({cells[0]['timestamp_sec']:.1f}s-{cells[-1]['timestamp_sec']:.1f}s)"
            )
            print(f"           -> {tile_path}")

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if layout["mode"] == "single":
            metadata = build_single_metadata(context, options, tile_entries[0], kept_frames_dir)
        else:
            metadata = build_multi_metadata(context, options, tile_entries, kept_frames_dir)
        manifest_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"フレーム数: {len(frames)}")
        print(f"タイル数:   {len(chunks)}")
        print(f"manifest:   {manifest_path}")
    finally:
        if not options.keep_frames:
            shutil.rmtree(temp_root, ignore_errors=True)

    return {
        "output": str(output_path),
        "manifest_path": str(manifest_path),
        "tile_count": len(chunks),
        "frame_count": len(frames),
    }


# --- config（複数範囲一括）モード ---------------------------------------------


def load_config(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if "video" not in config:
        raise RuntimeError("config に video が必要です")
    if not config.get("ranges"):
        raise RuntimeError("config に ranges が必要です")
    return config


def resolve_video_path(config: dict[str, Any], config_dir: Path) -> Path:
    video_path = Path(config["video"]).expanduser()
    if not video_path.is_absolute():
        candidates = [
            (config_dir / video_path).resolve(),
            (Path.cwd() / video_path).resolve(),
        ]
        video_path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    if not video_path.exists():
        raise RuntimeError(f"動画ファイルが見つかりません: {video_path}")
    return video_path


def overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    start = max(float(left["start"]), float(right["start"]))
    end = min(float(left["end"]), float(right["end"]))
    if end <= start:
        return 0.0
    overlap = end - start
    left_span = float(left["end"]) - float(left["start"])
    right_span = float(right["end"]) - float(right["start"])
    smaller = min(left_span, right_span)
    return overlap / smaller if smaller > 0 else 0.0


def merge_range_entries(ranges: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    if not ranges:
        return []

    sorted_ranges = sorted(ranges, key=lambda item: (float(item["start"]), float(item["end"])))
    merged: list[dict[str, Any]] = [dict(sorted_ranges[0])]

    for current in sorted_ranges[1:]:
        previous = merged[-1]
        if overlap_ratio(previous, current) >= threshold:
            previous["start"] = min(float(previous["start"]), float(current["start"]))
            previous["end"] = max(float(previous["end"]), float(current["end"]))
            labels = [part for part in [previous.get("label"), current.get("label")] if part]
            if labels:
                previous["label"] = "_".join(dict.fromkeys(labels))
            previous["needs_followup"] = previous.get("needs_followup") or current.get("needs_followup")
            priority_rank = {"high": 3, "medium": 2, "low": 1}
            previous["priority"] = max(
                [previous.get("priority", "low"), current.get("priority", "low")],
                key=lambda value: priority_rank.get(value, 0),
            )
            continue
        merged.append(dict(current))

    return merged


def merge_defaults(config: dict[str, Any], range_entry: dict[str, Any]) -> dict[str, Any]:
    defaults = config.get("defaults", {})
    merged = {**defaults, **range_entry}
    for key in ("start", "end"):
        if key not in merged:
            raise RuntimeError(f"range に {key} が必要です: {merged}")
    return merged


def resolve_output_path(config: dict[str, Any], merged: dict[str, Any]) -> Path:
    if "output" in merged:
        output_path = Path(merged["output"]).expanduser()
    else:
        if "output_dir" in config:
            output_dir = Path(config["output_dir"]).expanduser()
        else:
            output_dir = Path("output") / "agentic_tiles"
        label = merged.get("label") or f"{merged['start']}_{merged['end']}"
        fps = merged.get("fps", 1)
        output_path = output_dir / f"{label}_fps{fps}.jpg"
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()
    return output_path


# config の range キーから tile_one_range が読む options 属性へのマップ
_RANGE_OPTION_KEYS = (
    "fps",
    "pad",
    "frames_per_tile",
    "width",
    "tile_width",
    "tile_height",
    "cols",
    "cell_width",
    "label_height",
    "gap",
    "quality",
    "ffmpeg_quality",
    "metadata_output",
    "keep_frames",
    "quiet_warnings",
)


def make_range_options(
    base: argparse.Namespace,
    video_path: Path,
    merged: dict[str, Any],
    output_path: Path,
) -> argparse.Namespace:
    options = argparse.Namespace(**vars(base))
    options.config = None
    options.video = str(video_path)
    options.start = float(merged["start"])
    options.end = float(merged["end"])
    options.output = str(output_path)
    for key in _RANGE_OPTION_KEYS:
        if key in merged and merged[key] is not None:
            setattr(options, key, merged[key])
    return options


def run_config_mode(options: argparse.Namespace) -> int:
    config_path = Path(options.config).expanduser().resolve()
    config = load_config(config_path)
    config_dir = config_path.parent
    video_path = resolve_video_path(config, config_dir)

    range_entries = config["ranges"]
    if options.merge_overlaps:
        range_entries = merge_range_entries(range_entries, options.overlap_threshold)
        print(f"範囲をマージ: {len(config['ranges'])} -> {len(range_entries)}")

    results: list[dict[str, Any]] = []
    for index, range_entry in enumerate(range_entries):
        merged = merge_defaults(config, range_entry)
        output_path = resolve_output_path(config, merged)
        label = merged.get("label")

        if options.dry_run:
            print(
                f"[{index}] {label or ''} start={merged['start']} end={merged['end']}"
                f" fps={merged.get('fps', options.fps)} -> {output_path}"
            )
            results.append(
                {
                    "index": index,
                    "label": label,
                    "status": "dry_run",
                    "output": str(output_path),
                    "start_sec": merged["start"],
                    "end_sec": merged["end"],
                }
            )
            continue

        print(f"[{index}] {label or ''} {merged['start']}-{merged['end']}s -> {output_path}")
        range_options = make_range_options(options, video_path, merged, output_path)
        result = tile_one_range(range_options)
        results.append(
            {
                "index": index,
                "label": label,
                "status": "ok",
                "output": result["output"],
                "manifest_path": result["manifest_path"],
                "start_sec": merged["start"],
                "end_sec": merged["end"],
            }
        )

    summary_path = Path(config.get("summary_output", "output/agentic_tiles/batch_summary.json"))
    if not summary_path.is_absolute():
        summary_path = (Path.cwd() / summary_path).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "video": str(video_path),
                "config": str(config_path),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"summary: {summary_path}")
    return 0


def main(argv: list[str]) -> int:
    options = parse_args(argv)
    if options.config:
        return run_config_mode(options)
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
