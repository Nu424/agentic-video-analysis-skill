#!/usr/bin/env python
"""フレーム抽出・タイル描画・manifest 構築。

2つの入口がある。

- `tile_one_range(options)`: 範囲を fps で等間隔サンプリングし、格子状タイルにする
- `tile_zoom(options, timestamps)`: 指定時刻をフル解像度で1枚ずつ切り出す
  （`crop` / `scale` で ROI の切り出しと整数倍拡大ができる）

どちらも `TileOptions`（argparse.Namespace 互換の属性を持つデータクラス）を受け取り、
manifest v2（`tiles` 配列 + `extraction` + `tiling`）を書き出す。
"""

from __future__ import annotations

import dataclasses
import math
import shutil
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from avs.common import (
    format_float_for_path,
    format_timestamp,
    load_font,
    probe_duration_sec,
    probe_video_size,
    run_command,
    write_json,
)

DEFAULT_FRAMES_PER_TILE = 12
DEFAULT_TILE_WIDTH = 1600
DEFAULT_TILE_HEIGHT = 900
DEFAULT_EXTRACT_WIDTH = 640
DEFAULT_QUALITY = 80
DEFAULT_ZOOM_QUALITY = 90
WARN_FRAMES_PER_TILE = 16
# 600秒を超える範囲では m:ss 形式のタイムスタンプラベルを使う
LONG_FORM_THRESHOLD_SEC = 600.0


@dataclass
class TileOptions:
    """タイル化の設定。CLI の argparse.Namespace と同じ属性名を持つ。

    `from_namespace` で CLI から、直接コンストラクタでテストから作る。
    """

    video: str | None = None
    config: str | None = None
    start: float = 0.0
    end: float | None = None
    pad: float = 0.0
    fps: float | None = 1.0
    timestamps: str | None = None
    crop: str | None = None  # zoom モードのみ。ffmpeg の crop 記法 "W:H:X:Y"
    scale: int = 1  # zoom モードのみ。整数倍拡大
    output: str | None = None
    metadata_output: str | None = None
    frames_per_tile: int = DEFAULT_FRAMES_PER_TILE
    cols: int | None = None
    cell_width: int | None = None
    tile_width: int = DEFAULT_TILE_WIDTH
    tile_height: int = DEFAULT_TILE_HEIGHT
    width: int | None = None
    label_height: int = 28
    gap: int = 2
    quality: int | None = None
    ffmpeg_quality: int = 5
    keep_frames: bool = False
    quiet_warnings: bool = False
    merge_overlaps: bool = False
    overlap_threshold: float = 0.5
    dry_run: bool = False

    @classmethod
    def from_namespace(cls, namespace: Any) -> "TileOptions":
        known = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in vars(namespace).items() if key in known})

    def copy(self, **overrides: Any) -> "TileOptions":
        return dataclasses.replace(self, **overrides)


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


def validate_options(options: TileOptions) -> dict[str, Any]:
    # crop / scale は zoom モード（--timestamps）専用
    if options.crop is not None or (1 if options.scale is None else int(options.scale)) != 1:
        raise RuntimeError(
            "--crop / --scale は zoom モード（--timestamps、config の range では timestamps）でのみ使えます"
        )
    # タイル化モードでの既定値解決（None は zoom モード判別のための未指定マーカ）
    if options.width is None:
        options.width = DEFAULT_EXTRACT_WIDTH
    if options.quality is None:
        options.quality = DEFAULT_QUALITY

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
    long_form_labels: bool = False,
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
            f"F{frame_index} t={format_timestamp(timestamp_sec, long_form_labels)}",
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


def build_manifest(
    context: dict[str, Any],
    options: TileOptions,
    tile_entries: list[dict[str, Any]],
    temp_frames_dir: Path | None,
    approach: str = "agentic_video_frame_tiles",
) -> dict[str, Any]:
    """常に multi 形（tiles 配列）で manifest を組み立てる（version 2）。

    単一タイルでも tiles 配列1件の形で出力する。消費側 (avs/analysis.py)
    は tiles と extraction しか読まないため、この統一による実影響はない。
    """
    first = tile_entries[0]
    return {
        "version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "approach": approach,
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
            "crop": options.crop,
            "scale": options.scale,
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


def tile_one_range(options: TileOptions) -> dict[str, Any]:
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
        long_form_labels = context["end_sec"] >= LONG_FORM_THRESHOLD_SEC

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
                long_form_labels,
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
        metadata = build_manifest(context, options, tile_entries, kept_frames_dir)
        write_json(manifest_path, metadata)

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


# --- zoom（特定時刻をフル解像度で1枚ずつ）モード -----------------------------


def parse_timestamps(spec: str) -> list[float]:
    values: list[float] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError as exc:
            raise RuntimeError(f"--timestamps の値が不正です: {token!r}") from exc
    if not values:
        raise RuntimeError("--timestamps に有効な時刻がありません")
    if any(value < 0 for value in values):
        raise RuntimeError("--timestamps は 0 以上を指定してください")
    return values


def parse_crop(spec: str) -> tuple[int, int, int, int]:
    """ffmpeg の crop 記法 `W:H:X:Y` を検証して (W, H, X, Y) にする。"""
    parts = [token.strip() for token in str(spec).split(":")]
    if len(parts) != 4:
        raise RuntimeError(f"--crop は W:H:X:Y の形式で指定してください: {spec!r}")
    try:
        width, height, left, top = (int(part) for part in parts)
    except ValueError as exc:
        raise RuntimeError(f"--crop の値は整数で指定してください: {spec!r}") from exc
    if width < 1 or height < 1:
        raise RuntimeError(f"--crop の W / H は 1 以上を指定してください: {spec!r}")
    if left < 0 or top < 0:
        raise RuntimeError(f"--crop の X / Y は 0 以上を指定してください: {spec!r}")
    return width, height, left, top


def build_frame_filters(
    crop: str | None,
    scale: int,
    extract_width: int | None,
) -> str | None:
    """zoom モードの `-vf` を組み立てる。適用順は crop -> scale(整数倍) -> width。

    `--width` が指定されていれば、従来どおり最後にその幅へリサイズする。
    どれも指定が無ければ None（`-vf` を付けない）。
    """
    filters: list[str] = []
    if crop:
        width, height, left, top = parse_crop(crop)
        filters.append(f"crop={width}:{height}:{left}:{top}")
    factor = 1 if scale is None else int(scale)
    if factor < 1:
        raise RuntimeError("--scale は 1 以上の整数を指定してください")
    if factor > 1:
        filters.append(f"scale=iw*{factor}:ih*{factor}")
    if extract_width:
        filters.append(f"scale={extract_width}:-2")
    return ",".join(filters) if filters else None


def extract_single_frame(
    video_path: Path,
    out_path: Path,
    timestamp_sec: float,
    extract_width: int | None,
    ffmpeg_quality: int,
    crop: str | None = None,
    scale: int = 1,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{timestamp_sec:.6f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
    ]
    filters = build_frame_filters(crop, scale, extract_width)
    if filters:
        args.extend(["-vf", filters])
    args.extend(["-q:v", str(ffmpeg_quality), str(out_path)])
    run_command(args, "ffmpeg (zoom)")
    if not out_path.exists():
        raise RuntimeError(f"フレーム抽出に失敗しました: t={timestamp_sec}s")


def render_zoom_image(
    frame_path: Path,
    frame_index: int,
    timestamp_sec: float,
    label_height: int,
    long_form_labels: bool,
) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(frame_path) as frame:
        frame_rgb = frame.convert("RGB")
        width, height = frame_rgb.size
        canvas = Image.new("RGB", (width, height + label_height), "#1a1a1a")
        canvas.paste(frame_rgb, (0, 0))

    draw = ImageDraw.Draw(canvas)
    font = load_font(max(12, min(28, round(label_height * 0.6))))
    draw.rectangle((0, height, width, height + label_height), fill=(0, 0, 0))
    draw.text(
        (6, height + max(1, round(label_height * 0.18))),
        f"F{frame_index} t={format_timestamp(timestamp_sec, long_form_labels)}",
        fill=(255, 255, 255),
        font=font,
    )
    cell = {
        "frame_index": frame_index,
        "timestamp_sec": round(timestamp_sec, 6),
        "filename": frame_path.name,
        "row": 0,
        "col": 0,
    }
    return canvas, {"cell_width": width, "cell_height": height, "cell": cell}


def default_zoom_output_path(video_path: Path, timestamps: list[float]) -> Path:
    stem = video_path.stem
    first = format_float_for_path(timestamps[0])
    return Path("output") / "agentic_tiles" / f"{stem}_zoom_{first}.jpg"


def tile_zoom(options: TileOptions, timestamps: list[float]) -> dict[str, Any]:
    """特定時刻をフル解像度で1枚ずつ抽出し、zoom manifest を書き出す。"""
    video_path = Path(options.video).expanduser().resolve()
    if not video_path.exists():
        raise RuntimeError(f"動画ファイルが見つかりません: {video_path}")

    # zoom の既定: リサイズなし・品質90（明示指定があれば尊重）
    extract_width = options.width  # None ならフル解像度
    quality = options.quality if options.quality is not None else DEFAULT_ZOOM_QUALITY
    scale = 1 if options.scale is None else int(options.scale)
    # 形式エラーはフレーム抽出の前に出す
    build_frame_filters(options.crop, scale, extract_width)

    duration_sec = probe_duration_sec(video_path)
    source_width, source_height = probe_video_size(video_path)
    for ts in timestamps:
        if ts >= duration_sec:
            raise RuntimeError(f"--timestamps の {ts}s が動画長 {duration_sec:.3f}s を超えています")

    output_path = (
        Path(options.output).expanduser().resolve()
        if options.output
        else default_zoom_output_path(video_path, timestamps).resolve()
    )
    layout = resolve_output_layout(output_path, len(timestamps))
    manifest_path = (
        Path(options.metadata_output).expanduser().resolve()
        if options.metadata_output
        else layout["manifest_path"]
    )
    long_form_labels = max(timestamps) >= LONG_FORM_THRESHOLD_SEC

    temp_root = Path(tempfile.mkdtemp(prefix="agentic_video_zoom_"))
    print(f"入力動画: {video_path}")
    print(f"ズーム時刻: {', '.join(f'{ts:g}' for ts in timestamps)}")

    tile_entries: list[dict[str, Any]] = []
    try:
        for index, ts in enumerate(timestamps):
            raw_frame = temp_root / f"zoom_{index:03d}.jpg"
            extract_single_frame(
                video_path,
                raw_frame,
                ts,
                extract_width,
                options.ffmpeg_quality,
                crop=options.crop,
                scale=scale,
            )
            image, meta = render_zoom_image(raw_frame, index, ts, options.label_height, long_form_labels)
            tile_path = layout["tile_paths"][index]
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(tile_path, quality=quality)

            tile_entries.append(
                {
                    "tile_index": index,
                    "filename": tile_path.name,
                    "path": str(tile_path),
                    "start_frame": index,
                    "end_frame": index,
                    "start_timestamp_sec": round(ts, 6),
                    "end_timestamp_sec": round(ts, 6),
                    "frame_count": 1,
                    "grid": {"cols": 1, "rows": 1},
                    "cell_width": meta["cell_width"],
                    "cell_height": meta["cell_height"],
                    "tile_width": image.width,
                    "tile_height": image.height,
                    "cells": [meta["cell"]],
                }
            )
            print(f"  ズーム {index}: t={ts:g}s -> {tile_path}")

        context = {
            "video_path": video_path,
            "duration_sec": duration_sec,
            "source_width": source_width,
            "source_height": source_height,
            "requested_start_sec": min(timestamps),
            "requested_end_sec": max(timestamps),
            "start_sec": min(timestamps),
            "end_sec": max(timestamps),
        }
        # build_manifest 用に options の派生値を整える
        zoom_options = options.copy(width=extract_width, fps=None, scale=scale)
        manifest = build_manifest(
            context, zoom_options, tile_entries, None, approach="agentic_video_frame_zoom"
        )
        manifest["extraction"]["timestamps"] = [round(ts, 6) for ts in timestamps]
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(manifest_path, manifest)
        print(f"manifest:   {manifest_path}")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    return {
        "output": str(output_path),
        "manifest_path": str(manifest_path),
        "tile_count": len(timestamps),
        "frame_count": len(timestamps),
    }
