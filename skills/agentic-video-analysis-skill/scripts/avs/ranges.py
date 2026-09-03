#!/usr/bin/env python
"""範囲定義 config（複数範囲の一括タイル化）の読み込みと実行。

- `load_config` / `resolve_video_path`: config の検証とパス解決
- `merge_range_entries`: 重なる範囲のマージ（label / note / priority も合成）
- `merge_defaults`: `defaults` と range エントリの合成
- `resolve_output_path`: 出力先の決定
- `make_range_options`: range エントリ -> `TileOptions`
- `run_config_mode`: 全範囲を順にタイル化し `batch_summary.json` を書く（範囲単位で失敗を隔離）
- `plan_full_coverage`: overview の候補から全区間カバーの range 計画を作る（純粋関数。tiling 非依存）
- `resolve_coverage`: `--coverage auto` を動画長で full / priority に解決する
"""

from __future__ import annotations

import math
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from avs.common import read_json, safe_slug, write_json
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


def _range_label_fallback(merged: dict[str, Any]) -> str:
    """label 省略時のファイル名フォールバック（zoom / start_end）。"""
    if merged.get("timestamps"):
        return "zoom"
    return f"{merged['start']}_{merged['end']}"


def range_slug(merged: dict[str, Any]) -> str:
    """range の label をファイル名として安全な slug にする（`safe_slug` 経由）。

    元の `label` はここでは変更しない。ファイル名にだけ slug を使う。
    """
    fallback = _range_label_fallback(merged)
    label = merged.get("label") or fallback
    return safe_slug(str(label), fallback)


def resolve_output_path(config: dict[str, Any], merged: dict[str, Any]) -> Path:
    if "output" in merged:
        output_path = Path(merged["output"]).expanduser()
    else:
        if "output_dir" in config:
            output_dir = Path(config["output_dir"]).expanduser()
        else:
            output_dir = Path("output") / "agentic_tiles"
        slug = range_slug(merged)
        if merged.get("timestamps"):
            output_path = output_dir / f"{slug}_zoom.jpg"
        else:
            fps = merged.get("fps", 1)
            output_path = output_dir / f"{slug}_fps{fps}.jpg"
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()
    return output_path


# config の range キーから tile_one_range が読む options 属性へのマップ
_RANGE_OPTION_KEYS = (
    "fps",
    "pad",
    "crop",  # zoom range（timestamps 付き）のみ有効。ffmpeg の crop 記法 "W:H:X:Y"
    "scale",  # zoom range のみ有効。整数倍拡大
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


def _check_range_slug_collisions(merged_entries: list[dict[str, Any]]) -> None:
    """slug がファイル名を決める（= 明示 `output` が無い）エントリ同士の衝突を起動時に検出する。"""
    seen: dict[str, dict[str, Any]] = {}
    for merged in merged_entries:
        if "output" in merged:
            continue  # 明示 output は slug を使わないので対象外
        slug = range_slug(merged)
        if slug in seen:
            raise RuntimeError(
                f"range の label がファイル名で衝突します: slug={slug!r}"
                f"（label={seen[slug].get('label')!r} と label={merged.get('label')!r}）。"
                "label を分けてください"
            )
        seen[slug] = merged


def run_config_mode(options: TileOptions) -> int:
    config_path = Path(options.config).expanduser().resolve()
    config = load_config(config_path)
    config_dir = config_path.parent
    video_path = resolve_video_path(config, config_dir)

    range_entries = config["ranges"]
    if options.merge_overlaps:
        range_entries = merge_range_entries(range_entries, options.overlap_threshold)
        print(f"範囲をマージ: {len(config['ranges'])} -> {len(range_entries)}")

    merged_entries = [merge_defaults(config, entry) for entry in range_entries]
    _check_range_slug_collisions(merged_entries)

    results: list[dict[str, Any]] = []
    for index, merged in enumerate(merged_entries):
        output_path = resolve_output_path(config, merged)
        label = merged.get("label")
        slug = range_slug(merged)
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
                    "slug": slug,
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
        start_sec = min(timestamps) if is_zoom else merged["start"]
        end_sec = max(timestamps) if is_zoom else merged["end"]
        try:
            if is_zoom:
                result = tile_zoom(range_options, timestamps)
            else:
                result = tile_one_range(range_options)
        except Exception as error:  # noqa: BLE001 - 範囲ごとに失敗を隔離して続行する
            print(f"  [失敗] {label or index}: {error}")
            results.append(
                {
                    "index": index,
                    "label": label,
                    "slug": slug,
                    "note": note,
                    "status": "error",
                    "mode": "zoom" if is_zoom else "tile",
                    "error": str(error),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                }
            )
            continue
        results.append(
            {
                "index": index,
                "label": label,
                "slug": slug,
                "note": note,
                "status": "ok",
                "mode": "zoom" if is_zoom else "tile",
                "output": result["output"],
                "manifest_path": result["manifest_path"],
                "start_sec": start_sec,
                "end_sec": end_sec,
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

    processed = [entry for entry in results if entry["status"] != "dry_run"]
    error_count = sum(1 for entry in processed if entry["status"] == "error")
    if error_count:
        print(f"失敗した範囲: {error_count}/{len(processed)} 件")
    if processed and error_count == len(processed):
        return 1
    return 0


# --- 全区間カバー計画（plan_full_coverage） -------------------------------------
#
# `plan_ranges.py` から呼ばれる純粋関数。tiling / backend に依存しない。
# 入力の candidates[] は overview 解析結果の `candidates`（P3 でスキーマが確定する）。
# `start_sec` / `end_sec` だけを必須とし、他のキーは `.get` で寛容に読む。

# `--coverage auto` の分岐点。これ以下なら full、超えたら priority（§4.3）
DEFAULT_FULL_COVERAGE_MAX_SEC = 600.0

_PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}
_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
_COVERAGE_MODES = ("full", "priority", "high-only")

# 秒の比較に使う許容誤差（浮動小数の丸め対策）
_EPS = 1e-9


def resolve_coverage(
    coverage: str,
    duration_sec: float,
    full_coverage_max_sec: float = DEFAULT_FULL_COVERAGE_MAX_SEC,
) -> str:
    """`auto` を動画長で解決する（`full_coverage_max_sec` 以下なら full、超えたら priority）。"""
    if coverage != "auto":
        return coverage
    return "full" if duration_sec <= full_coverage_max_sec else "priority"


def _slugify(text: str | None, max_len: int = 24) -> str:
    """ラベルに使う安全な断片を作る（英数字とアンダースコアのみ、小文字）。"""
    if not text:
        return ""
    slug = _SLUG_RE.sub("_", text.strip()).strip("_").lower()
    return slug[:max_len].strip("_")


def _max_priority(left: str, right: str) -> str:
    return max([left, right], key=lambda value: _PRIORITY_RANK.get(value, 0))


def _merge_text_lists(left: list[str] | None, right: list[str] | None) -> list[str]:
    combined = list(left or []) + list(right or [])
    return list(dict.fromkeys(item for item in combined if item))


def _normalize_candidate(candidate: dict[str, Any], duration_sec: float) -> dict[str, Any] | None:
    raw_start = candidate.get("start_sec", candidate.get("start"))
    raw_end = candidate.get("end_sec", candidate.get("end"))
    if raw_start is None or raw_end is None:
        raise RuntimeError(f"candidate に start_sec/end_sec が必要です: {candidate}")
    start = max(0.0, float(raw_start))
    end = min(float(duration_sec), float(raw_end))
    if end <= start:
        return None  # 動画範囲外、または長さゼロ以下の候補は捨てる
    title = candidate.get("title")
    reason = candidate.get("reason")
    return {
        "start": start,
        "end": end,
        "priority": candidate.get("priority") or "medium",
        "reasons": [reason] if reason else [],
        "titles": [title] if title else [],
        "source": "overview",
    }


def _merge_candidates(items: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    if not items:
        return []
    merged: list[dict[str, Any]] = [dict(items[0])]
    for current in items[1:]:
        previous = merged[-1]
        if overlap_ratio(previous, current) >= threshold:
            previous["start"] = min(previous["start"], current["start"])
            previous["end"] = max(previous["end"], current["end"])
            previous["priority"] = _max_priority(previous["priority"], current["priority"])
            previous["reasons"] = _merge_text_lists(previous["reasons"], current["reasons"])
            previous["titles"] = _merge_text_lists(previous["titles"], current["titles"])
            continue
        merged.append(dict(current))
    return merged


def _split_span(start: float, end: float, max_range_sec: float) -> list[tuple[float, float]]:
    span = end - start
    if span <= 0:
        return []
    pieces = max(1, math.ceil(span / max_range_sec - 1e-9))
    width = span / pieces
    return [(start + index * width, start + (index + 1) * width) for index in range(pieces)]


def _split_long_items(items: list[dict[str, Any]], max_range_sec: float) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        span = item["end"] - item["start"]
        if span <= max_range_sec + 1e-9:
            result.append(dict(item))
            continue
        for piece_start, piece_end in _split_span(item["start"], item["end"], max_range_sec):
            piece = dict(item)
            piece["start"] = piece_start
            piece["end"] = piece_end
            result.append(piece)
    return result


def _absorb(target: dict[str, Any], other: dict[str, Any]) -> None:
    """`other` の priority / reasons / titles を `target` に取り込む（時間は呼び出し側が決める）。"""
    target["priority"] = _max_priority(target["priority"], other["priority"])
    target["reasons"] = _merge_text_lists(target["reasons"], other["reasons"])
    target["titles"] = _merge_text_lists(target["titles"], other["titles"])
    if other.get("source") == "overview":
        target["source"] = "overview"


def _merge_short_items(
    items: list[dict[str, Any]],
    min_range_sec: float,
    min_gap_sec: float,
    max_range_sec: float,
) -> list[dict[str, Any]]:
    """`min_range_sec` 未満の範囲を、隙間が `min_gap_sec` 未満の隣接範囲にだけマージする。

    隙間が `min_gap_sec` 以上離れた候補同士は無関係な出来事の可能性が高いので混ぜない
    （その隙間は後段の `_fill_gaps` が gap 範囲として埋める）。隣接範囲が無ければ
    短いまま残す。

    マージ後の長さが `max_range_sec` を超えるときはマージしない（1 回の呼び出しに詰め込む
    秒数の上限は実測に基づく既定値なので、短い範囲を潰すために破らない。§4.3-2）。
    短い範囲はそのまま残り、隙間は `_fill_gaps` が閉じる。
    """
    if len(items) <= 1:
        return [dict(item) for item in items]
    result = [dict(item) for item in items]
    index = 0
    while index < len(result):
        current = result[index]
        span = current["end"] - current["start"]
        if span >= min_range_sec - _EPS or len(result) == 1:
            index += 1
            continue

        if index + 1 < len(result):
            nxt = result[index + 1]
            if (
                nxt["start"] - current["end"] < min_gap_sec - _EPS
                and nxt["end"] - current["start"] <= max_range_sec + _EPS
            ):
                nxt["start"] = current["start"]
                _absorb(nxt, current)
                del result[index]
                continue  # 結合先(次の要素)を同じ index で再チェックする

        if index > 0:
            prev = result[index - 1]
            if (
                current["start"] - prev["end"] < min_gap_sec - _EPS
                and current["end"] - prev["start"] <= max_range_sec + _EPS
            ):
                prev["end"] = current["end"]
                _absorb(prev, current)
                del result[index]
                index -= 1
                continue

        index += 1  # 隣接する範囲が無い（またはマージすると長すぎる）ので短いまま残す
    return result


def _gap_item(start: float, end: float) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "priority": "low",
        "reasons": [],
        "titles": [],
        "source": "gap",
    }


def _fill_gaps(
    items: list[dict[str, Any]], duration_sec: float, min_gap_sec: float, max_range_sec: float
) -> list[dict[str, Any]]:
    """`[0, duration]` を隙間なく覆うように範囲を並べ直す。

    **正の隙間は長さに関係なく必ず閉じる**（§4.3-3,4。「範囲と範囲の隙間」は動画末尾と並ぶ
    取りこぼしの発生源なので、1 秒未満でも解析対象から外さない）。閉じ方は 2 通り:

    - 隙間が `min_gap_sec` 以上: `gap_NN` として独立した範囲にする
      （`max_range_sec` で分割し、`priority=low` を付ける）
    - 隙間が `min_gap_sec` 未満: 直前の範囲の `end` を次の `start` まで伸ばして吸収する
      （1 秒に満たない断片に独立した API 呼び出しを割り当てない）。
      直前が無い（＝動画の先頭）ときは、代わりに次の範囲の `start` を 0 まで戻す。

    重なりが残っている範囲（`overlap_threshold` 未満でマージされなかったもの）は、
    後ろ側の `start` を直前の `end` まで詰めて重なりを解消する。完全に飲み込まれた範囲は
    priority / note だけを直前の範囲に引き継いで捨てる。
    """
    filled: list[dict[str, Any]] = []
    cursor = 0.0
    for item in items:
        current = dict(item)
        gap = current["start"] - cursor
        if gap > _EPS:
            if gap >= min_gap_sec - _EPS:
                for piece_start, piece_end in _split_span(cursor, current["start"], max_range_sec):
                    filled.append(_gap_item(piece_start, piece_end))
            elif filled:
                filled[-1]["end"] = current["start"]  # 直前を伸ばして吸収する
            else:
                current["start"] = cursor  # 先頭の短い隙間は次の範囲を前に伸ばす
        elif gap < -_EPS:
            current["start"] = cursor  # 重なりを解消する（カバー範囲は直前が持っている）

        if current["end"] - current["start"] <= _EPS:
            if filled:  # 直前に飲み込まれた範囲。note と priority だけ引き継ぐ
                _absorb(filled[-1], current)
            continue

        filled.append(current)
        cursor = current["end"]

    tail = duration_sec - cursor
    if tail > _EPS:
        if tail >= min_gap_sec - _EPS or not filled:
            for piece_start, piece_end in _split_span(cursor, duration_sec, max_range_sec):
                filled.append(_gap_item(piece_start, piece_end))
        else:
            filled[-1]["end"] = duration_sec
    return filled


def _ensure_bounds(items: list[dict[str, Any]], duration_sec: float) -> list[dict[str, Any]]:
    """先頭 `start == 0` / 末尾 `end == duration` を保証する（`_fill_gaps` の後の保険）。"""
    if not items:
        return [_gap_item(0.0, duration_sec)]
    items[0]["start"] = 0.0
    items[-1]["end"] = duration_sec
    return items


def _assign_labels(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cand_index = 0
    gap_index = 0
    labeled: list[dict[str, Any]] = []
    for item in items:
        new_item = dict(item)
        if item["source"] == "overview":
            slug = _slugify(item["titles"][0] if item.get("titles") else None)
            new_item["label"] = f"cand_{cand_index:02d}" + (f"_{slug}" if slug else "")
            cand_index += 1
        else:
            new_item["label"] = f"gap_{gap_index:02d}"
            gap_index += 1
        labeled.append(new_item)
    return labeled


def _build_note(item: dict[str, Any]) -> str | None:
    parts = []
    if item.get("titles"):
        parts.append(" / ".join(item["titles"]))
    if item.get("reasons"):
        parts.append(" / ".join(item["reasons"]))
    combined = " / ".join(part for part in parts if part)
    return combined or None


def _finalize_entry(item: dict[str, Any], fps: float | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "label": item["label"],
        "start": round(item["start"], 2),
        "end": round(item["end"], 2),
        "priority": item["priority"],
        "source": item["source"],
    }
    note = _build_note(item)
    if note:
        entry["note"] = note
    if fps is not None:
        entry["fps"] = fps
    return entry


def _verify_full_coverage(entries: list[dict[str, Any]], duration_sec: float) -> None:
    """全区間カバーの不変条件を検証する。破れていれば `RuntimeError`（§4.3-4）。

    - `ranges[0].start == 0`
    - `ranges[-1].end == duration`
    - `ranges[i].end == ranges[i+1].start`（隙間も重なりも無い）

    ここで落とすのは、隙間に落ちた出来事を「解析したつもり」で見逃すより、
    計画の段階で止めた方が安いから。`coverage=high-only` で範囲を捨てたときは
    連続性が成り立たないので、呼び出し側がこの検証を飛ばす。
    検証は丸めた後の値（実際に config に書かれる値）に対して行う。
    """
    if not entries:
        raise RuntimeError("範囲計画が空です（全区間カバーの不変条件を満たしません）")
    expected_end = round(float(duration_sec), 2)
    if abs(float(entries[0]["start"])) > _EPS:
        raise RuntimeError(
            f"範囲計画の先頭が 0 から始まっていません: start={entries[0]['start']}"
        )
    if abs(float(entries[-1]["end"]) - expected_end) > _EPS:
        raise RuntimeError(
            f"範囲計画の末尾が動画長と一致しません: end={entries[-1]['end']} / duration={expected_end}"
        )
    for left, right in pairwise(entries):
        if abs(float(left["end"]) - float(right["start"])) > _EPS:
            raise RuntimeError(
                "範囲計画が連続していません: "
                f"{left['label']} end={left['end']} -> {right['label']} start={right['start']}"
            )


def plan_full_coverage(
    candidates: list[dict[str, Any]],
    duration_sec: float,
    *,
    max_range_sec: float = 8.0,
    min_range_sec: float = 2.0,
    min_gap_sec: float = 1.0,
    overlap_threshold: float = 0.5,
    coverage: str = "full",
    detail_fps: float = 5.0,
    low_fps: float = 1.0,
    pad: float = 1.0,
) -> dict[str, Any]:
    """overview の候補から、動画全体を隙間なく覆う range 計画（config 形式）を作る。

    手順（§4.3）: クリップ -> overlap マージ -> 長い範囲の等分割 -> 短い範囲のマージ
    -> 隙間埋め（`min_gap_sec` 以上は gap_NN、未満は直前の範囲が吸収）
    -> 先頭 start==0 / 末尾 end==duration の保証 -> `max_range_sec` 超過の再分割
    -> coverage に応じた fps 付与（high-only は low を落として dropped[] に記録）
    -> 不変条件の検証（連続性。high-only で範囲を捨てたときは検証しない）。

    出力の範囲は **必ず連続する**（`ranges[i].end == ranges[i+1].start`）。
    区間の隙間は候補の取りこぼし要因なので、1 秒未満でも解析対象から外さない。

    `video` / `output_dir` / `summary_output` は None のまま返す（CLI が埋める）。
    """
    if duration_sec <= 0:
        raise RuntimeError("duration_sec は 0 より大きい値を指定してください")
    if coverage not in _COVERAGE_MODES:
        raise RuntimeError(
            f"coverage は {' / '.join(_COVERAGE_MODES)} のいずれかを指定してください: {coverage}"
        )
    if max_range_sec <= 0:
        raise RuntimeError("max_range_sec は 0 より大きい値を指定してください")

    normalized = [
        item
        for item in (_normalize_candidate(candidate, duration_sec) for candidate in candidates)
        if item is not None
    ]
    normalized.sort(key=lambda item: (item["start"], item["end"]))

    merged = _merge_candidates(normalized, overlap_threshold)
    split = _split_long_items(merged, max_range_sec)
    short_merged = _merge_short_items(split, min_range_sec, min_gap_sec, max_range_sec)
    filled = _fill_gaps(short_merged, duration_sec, min_gap_sec, max_range_sec)
    bounded = _ensure_bounds(filled, duration_sec)
    # 隙間の吸収で `max_range_sec` を超えた範囲をここで再分割する。
    # 等分割は連続性を壊さない（分割片の end が次の片の start と一致する）。
    resplit = _split_long_items(bounded, max_range_sec)
    labeled = _assign_labels(resplit)

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in labeled:
        priority = item["priority"]
        if coverage == "high-only" and priority == "low":
            dropped.append(_finalize_entry(item))
            continue
        if coverage == "full":
            fps = detail_fps
        elif coverage == "priority":
            fps = detail_fps if priority in ("high", "medium") else low_fps
        else:  # high-only: 残るのは high/medium のみ
            fps = detail_fps
        kept.append(_finalize_entry(item, fps=fps))

    # high-only で範囲を捨てたときは連続にならないので検証しない（§4.3-5）
    if not dropped:
        _verify_full_coverage(kept, duration_sec)

    return {
        "video": None,
        "output_dir": None,
        "defaults": {"fps": detail_fps, "pad": pad, "frames_per_tile": 12},
        "ranges": kept,
        "summary_output": None,
        "plan": {
            "coverage": coverage,
            "duration_sec": round(float(duration_sec), 2),
            "n_ranges": len(kept),
            "dropped": dropped,
        },
    }
