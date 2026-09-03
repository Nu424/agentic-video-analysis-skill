"""avs.tiling のテスト。

合成動画を使うテストは ffmpeg が要る（conftest の synth_video フィクスチャが skip を出す）。
純粋な計算部分は動画なしで検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avs.common import format_timestamp
from avs.tiling import (
    DEFAULT_EXTRACT_WIDTH,
    LONG_FORM_THRESHOLD_SEC,
    TileOptions,
    apply_pad,
    auto_cols,
    chunk_frames,
    compute_grid,
    parse_timestamps,
    resolve_output_layout,
    tile_one_range,
    tile_zoom,
)


def _read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- 計算部分（動画不要） -----------------------------------------------------


def test_format_timestamp_short_form():
    assert format_timestamp(38.0) == "38.0s"
    assert format_timestamp(0.0) == "0.0s"
    assert format_timestamp(1234.5) == "1234.5s"


def test_format_timestamp_long_form():
    # 600秒超は m:ss.s 形式にする（タイル上のラベルが読みやすくなる）
    assert format_timestamp(1234.5, long_form=True) == "20:34.5"
    assert format_timestamp(38.0, long_form=True) == "0:38.0"
    assert format_timestamp(LONG_FORM_THRESHOLD_SEC, long_form=True) == "10:00.0"


def test_apply_pad_clips_to_video_bounds():
    assert apply_pad(4.0, 9.0, 1.0, 30.0) == (3.0, 10.0)
    # 先頭・末尾は動画の範囲でクリップされる
    assert apply_pad(0.5, 29.5, 2.0, 30.0) == (0.0, 30.0)


def test_apply_pad_rejects_empty_range():
    with pytest.raises(RuntimeError):
        apply_pad(5.0, 6.0, 0.0, 4.0)


def test_chunk_frames_and_grid():
    frames = [Path(f"f{i}.jpg") for i in range(14)]
    chunks = chunk_frames(frames, 6)
    assert [len(chunk) for chunk in chunks] == [6, 6, 2]
    assert auto_cols(12) == 4
    assert compute_grid(12, None, 12) == (4, 3)
    assert compute_grid(2, None, 12) == (2, 1)
    assert compute_grid(7, 3, 12) == (3, 3)


def test_resolve_output_layout_single_vs_multi(tmp_path):
    single = resolve_output_layout(tmp_path / "out.jpg", 1)
    assert single["mode"] == "single"
    assert single["tile_paths"] == [tmp_path / "out.jpg"]
    assert single["manifest_path"] == tmp_path / "out.json"

    multi = resolve_output_layout(tmp_path / "out.jpg", 3)
    assert multi["mode"] == "multi"
    assert multi["output_dir"] == tmp_path / "out"
    assert [p.name for p in multi["tile_paths"]] == ["tile_000.jpg", "tile_001.jpg", "tile_002.jpg"]
    assert multi["manifest_path"] == tmp_path / "out" / "manifest.json"


def test_parse_timestamps():
    assert parse_timestamps("3.5, 7.0 ,9") == [3.5, 7.0, 9.0]
    with pytest.raises(RuntimeError):
        parse_timestamps("abc")
    with pytest.raises(RuntimeError):
        parse_timestamps("-1")


# --- タイル化（動画あり） -----------------------------------------------------


def test_tile_one_range_manifest(synth_video, tmp_path):
    options = TileOptions(
        video=str(synth_video),
        start=4.0,
        end=9.0,
        pad=1.0,
        fps=2.0,
        frames_per_tile=6,
        output=str(tmp_path / "range.jpg"),
        quiet_warnings=True,
    )
    result = tile_one_range(options)

    manifest = _read_manifest(Path(result["manifest_path"]))
    assert manifest["version"] == 2
    assert manifest["approach"] == "agentic_video_frame_tiles"

    extraction = manifest["extraction"]
    assert extraction["requested_start_sec"] == 4.0
    assert extraction["requested_end_sec"] == 9.0
    # pad 1.0 で 3.0-10.0s になる
    assert extraction["start_sec"] == 3.0
    assert extraction["end_sec"] == 10.0
    assert extraction["duration_sec"] == 7.0
    assert extraction["pad_sec"] == 1.0
    assert extraction["fps"] == 2.0
    assert extraction["extract_width"] == DEFAULT_EXTRACT_WIDTH
    # 7秒 x fps2 = 14枚 -> 6枚ずつで 3タイル
    assert extraction["frame_count_total"] == 14
    assert extraction["kept_frames_dir"] is None

    tiles = manifest["tiles"]
    assert len(tiles) == 3
    assert manifest["tiling"]["tile_count"] == 3
    assert [tile["frame_count"] for tile in tiles] == [6, 6, 2]

    # タイム スタンプは padded start + frame_index / fps
    assert tiles[0]["start_timestamp_sec"] == pytest.approx(3.0)
    assert tiles[0]["end_timestamp_sec"] == pytest.approx(5.5)
    assert tiles[1]["start_timestamp_sec"] == pytest.approx(6.0)
    assert tiles[1]["end_timestamp_sec"] == pytest.approx(8.5)
    assert tiles[2]["start_timestamp_sec"] == pytest.approx(9.0)
    assert tiles[2]["end_timestamp_sec"] == pytest.approx(9.5)
    assert [tile["start_frame"] for tile in tiles] == [0, 6, 12]
    assert [tile["end_frame"] for tile in tiles] == [5, 11, 13]

    # セルは左->右、上->下の順で時系列
    cells = tiles[0]["cells"]
    assert [cell["frame_index"] for cell in cells] == [0, 1, 2, 3, 4, 5]
    assert [(cell["row"], cell["col"]) for cell in cells[:4]] == [(0, 0), (0, 1), (0, 2), (1, 0)]
    assert cells[3]["timestamp_sec"] == pytest.approx(4.5)

    # 画像が実在する
    for tile in tiles:
        assert Path(tile["path"]).exists()


def test_tile_one_range_single_tile_layout(synth_video, tmp_path):
    options = TileOptions(
        video=str(synth_video),
        start=0.0,
        end=4.0,
        fps=1.0,
        frames_per_tile=12,
        output=str(tmp_path / "one.jpg"),
        quiet_warnings=True,
    )
    result = tile_one_range(options)

    # 1タイルのときは指定パスそのまま、manifest は横の .json
    assert Path(result["output"]) == (tmp_path / "one.jpg")
    assert Path(result["manifest_path"]) == (tmp_path / "one.json")
    manifest = _read_manifest(Path(result["manifest_path"]))
    assert len(manifest["tiles"]) == 1
    assert manifest["tiles"][0]["frame_count"] == 4


def test_tile_zoom_manifest(synth_video, tmp_path):
    options = TileOptions(
        video=str(synth_video),
        output=str(tmp_path / "zoom.jpg"),
    )
    result = tile_zoom(options, [3.5, 7.0])

    manifest = _read_manifest(Path(result["manifest_path"]))
    assert manifest["approach"] == "agentic_video_frame_zoom"
    assert manifest["extraction"]["timestamps"] == [3.5, 7.0]
    # zoom は fps の概念がなく、リサイズもしない
    assert manifest["extraction"]["fps"] is None
    assert manifest["extraction"]["extract_width"] is None
    assert manifest["extraction"]["start_sec"] == 3.5
    assert manifest["extraction"]["end_sec"] == 7.0

    tiles = manifest["tiles"]
    assert len(tiles) == 2
    assert [tile["frame_count"] for tile in tiles] == [1, 1]
    assert [tile["grid"] for tile in tiles] == [{"cols": 1, "rows": 1}] * 2
    assert tiles[0]["start_timestamp_sec"] == tiles[0]["end_timestamp_sec"] == 3.5
    assert tiles[1]["start_timestamp_sec"] == tiles[1]["end_timestamp_sec"] == 7.0
    # フル解像度（合成動画のサイズそのまま）
    assert tiles[0]["cell_width"] == 320
    assert tiles[0]["cell_height"] == 180
    for tile in tiles:
        assert Path(tile["path"]).exists()


def test_tile_zoom_rejects_timestamp_beyond_duration(synth_video, tmp_path):
    options = TileOptions(video=str(synth_video), output=str(tmp_path / "zoom.jpg"))
    with pytest.raises(RuntimeError):
        tile_zoom(options, [999.0])
