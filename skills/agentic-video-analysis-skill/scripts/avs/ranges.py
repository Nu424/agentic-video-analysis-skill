#!/usr/bin/env python
"""範囲定義 config（複数範囲の一括タイル化）の読み込みと実行。

- `load_config` / `resolve_video_path`: config の検証とパス解決
- `merge_range_entries`: 重なる範囲のマージ（label / note / priority も合成）
- `merge_defaults`: `defaults` と range エントリの合成
- `resolve_output_path`: 出力先の決定
- `make_range_options`: range エントリ -> `TileOptions`
- `run_config_mode`: 全範囲を順にタイル化し `batch_summary.json` を書く
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from avs.common import read_json, write_json
from avs.tiling import TileOptions, parse_timestamps, tile_one_range, tile_zoom


def load_config(config_path: Path) -> dict[str, Any]:
    config = read_json(config_path)
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
            notes = [part for part in [previous.get("note"), current.get("note")] if part]
            if notes:
                previous["note"] = " / ".join(dict.fromkeys(notes))
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
    if merged.get("timestamps"):
        return merged  # zoom range は start/end 不要
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
        if merged.get("timestamps"):
            label = merged.get("label") or "zoom"
            output_path = output_dir / f"{label}_zoom.jpg"
        else:
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


def parse_timestamps_value(value: Any) -> list[float]:
    """config の timestamps（配列 or カンマ区切り文字列）を float リストにする。"""
    if isinstance(value, str):
        return parse_timestamps(value)
    values = [float(item) for item in value]
    if not values:
        raise RuntimeError("timestamps が空です")
    return values


def make_range_options(
    base: TileOptions,
    video_path: Path,
    merged: dict[str, Any],
    output_path: Path,
) -> TileOptions:
    options = base.copy()
    options.config = None
    options.video = str(video_path)
    options.output = str(output_path)
    if not merged.get("timestamps"):
        options.start = float(merged["start"])
        options.end = float(merged["end"])
    for key in _RANGE_OPTION_KEYS:
        if key in merged and merged[key] is not None:
            setattr(options, key, merged[key])
    return options


def run_config_mode(options: TileOptions) -> int:
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
        note = merged.get("note")
        is_zoom = bool(merged.get("timestamps"))
        timestamps = parse_timestamps_value(merged["timestamps"]) if is_zoom else None
        span_desc = (
            f"timestamps={','.join(f'{ts:g}' for ts in timestamps)}"
            if is_zoom
            else f"{merged['start']}-{merged['end']}s"
        )

        if options.dry_run:
            print(f"[{index}] {label or ''} {span_desc} -> {output_path}" + (f" note={note}" if note else ""))
            results.append(
                {
                    "index": index,
                    "label": label,
                    "note": note,
                    "status": "dry_run",
                    "mode": "zoom" if is_zoom else "tile",
                    "output": str(output_path),
                    "start_sec": min(timestamps) if is_zoom else merged["start"],
                    "end_sec": max(timestamps) if is_zoom else merged["end"],
                }
            )
            continue

        print(f"[{index}] {label or ''} {span_desc} -> {output_path}")
        range_options = make_range_options(options, video_path, merged, output_path)
        if is_zoom:
            result = tile_zoom(range_options, timestamps)
        else:
            result = tile_one_range(range_options)
        results.append(
            {
                "index": index,
                "label": label,
                "note": note,
                "status": "ok",
                "mode": "zoom" if is_zoom else "tile",
                "output": result["output"],
                "manifest_path": result["manifest_path"],
                "start_sec": min(timestamps) if is_zoom else merged["start"],
                "end_sec": max(timestamps) if is_zoom else merged["end"],
            }
        )

    summary_path = Path(config.get("summary_output", "output/agentic_tiles/batch_summary.json"))
    if not summary_path.is_absolute():
        summary_path = (Path.cwd() / summary_path).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        summary_path,
        {
            "video": str(video_path),
            "config": str(config_path),
            "results": results,
        },
    )
    print(f"summary: {summary_path}")
    return 0
