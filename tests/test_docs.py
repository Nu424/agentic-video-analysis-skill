"""ドキュメントのコマンド例が実在するオプションだけで書かれているかを検査する。

対象は `SKILL.md` / `README.md` / `docs/WORKFLOW.md` の ```bash ブロック。
その中の `python .../<script>.py ...` 行を取り出し、行に現れる `--xxx` が
該当スクリプトの argparse に存在するかを確かめる（`<session>` のような
プレースホルダは値なので無視される）。`eval/score.py` と `eval/union_recall.py`
も対象にする。

実 API もファイル生成も行わない。argparse のパーサだけを取り出して照合する。
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "agentic-video-analysis-skill"
EVAL_DIR = REPO_ROOT / "eval"

if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

DOC_PATHS = (
    SKILL_DIR / "SKILL.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "WORKFLOW.md",
)

# ドキュメントに出てよいスクリプト名 -> (モジュール名, パーサ取得方法)
SCRIPT_MODULES = {
    "run_pipeline.py": ("run_pipeline", "parse_args"),
    "tile_video_frames.py": ("tile_video_frames", "parse_args"),
    "plan_ranges.py": ("plan_ranges", "parse_args"),
    "analyze.py": ("analyze", "parse_args"),
    "validate_analysis.py": ("validate_analysis", "parse_args"),
    "merge_analyses.py": ("merge_analyses", "parse_args"),
    "analyze_audio.py": ("analyze_audio", "parse_args"),
    "session_report.py": ("session_report", "parse_args"),
    "score.py": ("score", "build_arg_parser"),
    "union_recall.py": ("union_recall", "build_arg_parser"),
}

BASH_BLOCK_RE = re.compile(r"^```bash\s*$(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
SCRIPT_RE = re.compile(r"(?<![\w-])([A-Za-z_][\w-]*\.py)\b")
OPTION_RE = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*)")


# --- パーサの取得 -----------------------------------------------------------------


class _CapturedParser(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__("parser captured")
        self.parser = parser


def _parser_of(script_name: str) -> argparse.ArgumentParser:
    """CLI モジュールから argparse のパーサを取り出す。

    `parse_args` 型のスクリプトは `argparse.ArgumentParser.parse_args` を一時的に
    差し替えて、パーサ自身を例外で受け取る（必須引数の検査より前に抜けるので、
    引数なしで呼んでも失敗しない）。
    """
    module_name, kind = SCRIPT_MODULES[script_name]
    module = importlib.import_module(module_name)
    if kind == "build_arg_parser":
        return module.build_arg_parser()

    original = argparse.ArgumentParser.parse_args

    def spy(self, args=None, namespace=None):  # noqa: ANN001, ANN202
        raise _CapturedParser(self)

    argparse.ArgumentParser.parse_args = spy  # type: ignore[method-assign]
    try:
        module.parse_args([])
    except _CapturedParser as captured:
        return captured.parser
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]
    raise AssertionError(f"{script_name} の parse_args がパーサを使いませんでした")


def _known_options(script_name: str) -> set[str]:
    options: set[str] = set()
    for action in _parser_of(script_name)._actions:
        options.update(action.option_strings)
    return options


# --- ドキュメントからのコマンド抽出 ---------------------------------------------------


def _logical_lines(block: str) -> list[str]:
    """行末 `\\` の継続を1行にまとめ、先頭のコメント記号を落とす。"""
    lines: list[str] = []
    buffer = ""
    for raw in block.splitlines():
        line = raw.strip()
        line = re.sub(r"^#\s*", "", line)  # コメント行のコマンドも検査対象にする
        if not line:
            continue
        if line.endswith("\\"):
            buffer += line[:-1].rstrip() + " "
            continue
        lines.append((buffer + line).strip())
        buffer = ""
    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def iter_doc_commands() -> list[tuple[Path, str, str]]:
    """(ドキュメント, スクリプト名, スクリプト以降のコマンド文字列) を返す。"""
    found: list[tuple[Path, str, str]] = []
    for doc_path in DOC_PATHS:
        text = doc_path.read_text(encoding="utf-8")
        for block in BASH_BLOCK_RE.findall(text):
            for line in _logical_lines(block):
                match = SCRIPT_RE.search(line)
                if match is None:
                    continue
                found.append((doc_path, match.group(1), line[match.end() :]))
    return found


DOC_COMMANDS = iter_doc_commands()


# --- テスト ---------------------------------------------------------------------


def test_docs_contain_commands() -> None:
    """3つのドキュメントすべてからコマンド例を取り出せている。"""
    assert DOC_COMMANDS, "ドキュメントから python コマンドを1件も抽出できませんでした"
    documented = {doc_path for doc_path, _, _ in DOC_COMMANDS}
    for doc_path in DOC_PATHS:
        assert doc_path in documented, f"{doc_path.name} に bash のコマンド例がありません"


def test_only_known_scripts_are_documented() -> None:
    """廃止・改名されたスクリプト名が残っていない。"""
    unknown = sorted(
        {script for _, script, _ in DOC_COMMANDS if script not in SCRIPT_MODULES}
    )
    assert not unknown, f"未知のスクリプトがドキュメントに出ています: {unknown}"


def test_pipeline_and_eval_are_documented() -> None:
    """標準ドライバと採点スクリプトの例が載っている（案内としての最低限）。"""
    scripts = {script for _, script, _ in DOC_COMMANDS}
    assert "run_pipeline.py" in scripts
    assert "score.py" in scripts


@pytest.mark.parametrize(
    ("doc_path", "script", "tail"),
    DOC_COMMANDS,
    ids=[f"{doc.name}:{script}:{index}" for index, (doc, script, _) in enumerate(DOC_COMMANDS)],
)
def test_documented_options_exist(doc_path: Path, script: str, tail: str) -> None:
    """コマンド例の `--xxx` が該当スクリプトの argparse に存在する。"""
    known = _known_options(script)
    used = OPTION_RE.findall(tail)
    unknown = sorted({option for option in used if option not in known})
    assert not unknown, (
        f"{doc_path.name} の {script} のコマンド例に存在しないオプションがあります: "
        f"{unknown}（実在するのは {sorted(known)}）"
    )
