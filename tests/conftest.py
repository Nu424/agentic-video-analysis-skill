"""pytest 共通設定。

- `scripts/` を sys.path に入れて `avs` パッケージを import 可能にする
  （CLI 起動時に `scripts/` が sys.path[0] になるのと同じ状態を作る）
- ffmpeg があれば合成動画を1本だけ作る（session スコープ）。無ければ skip
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "agentic-video-analysis-skill" / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 合成動画の仕様（テストの期待値はこの値から計算する）
SYNTH_DURATION_SEC = 30.0
SYNTH_RATE = 10
SYNTH_WIDTH = 320
SYNTH_HEIGHT = 180


def _ffmpeg_missing() -> bool:
    return shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None


@pytest.fixture(scope="session")
def synth_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """色パターン + 時間変化のある合成動画。ffmpeg が無ければ skip。"""
    if _ffmpeg_missing():
        pytest.skip("ffmpeg/ffprobe が無いため動画を使うテストをスキップします")

    path = tmp_path_factory.mktemp("synth") / "synth.mp4"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={SYNTH_WIDTH}x{SYNTH_HEIGHT}:rate={SYNTH_RATE}:duration={SYNTH_DURATION_SEC:g}",
        "-pix_fmt",
        "yuv420p",
        "-y",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0 or not path.exists():
        pytest.skip(f"合成動画を作成できませんでした: {(completed.stderr or '').strip()}")
    return path
