#!/usr/bin/env python
"""
タイル manifest を aitool recognize-image で解析するCLI。
1つの manifest = 1回の API 呼び出し（含まれる全タイルをまとめて渡す）。複数 manifest をまとめて処理できる。

単一 manifest:
  python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py \
    --manifest output/agentic_sessions/example/overview/video_full_fps0.5/manifest.json \
    --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt \
    --output output/agentic_sessions/example/overview/video_full_fps0.5_analysis.txt

複数 manifest（タイル化 summary から自動列挙）:
  python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py \
    --summary output/agentic_sessions/example/candidates/batch_summary.json \
    --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="manifest.json の全タイルをまとめて aitool recognize-image に渡します。複数 manifest 可。"
    )
    parser.add_argument(
        "-m",
        "--manifest",
        nargs="+",
        default=None,
        help="タイル manifest.json のパス（複数指定可）",
    )
    parser.add_argument(
        "-s",
        "--summary",
        default=None,
        help="タイル化の batch_summary.json。results[].manifest_path から manifest を自動列挙する",
    )
    parser.add_argument("-p", "--prompt", required=True, help="プロンプトテキストファイルのパス")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="解析結果の出力パス（manifest が1件のときのみ有効）。省略時は manifest 横に自動命名",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="複数 manifest の出力をまとめるディレクトリ。各 manifest を <名前>_analysis.txt で出力",
    )
    parser.add_argument(
        "--model",
        default="google/gemini-3.5-flash",
        help="使用モデル（既定: google/gemini-3.5-flash）",
    )
    parser.add_argument(
        "--aitool",
        default=None,
        help="aitool コマンドのパス。省略時は PATH から検索",
    )
    parser.add_argument("--api-key", default=None, help="OpenRouter APIキー（省略時は環境変数）")
    parser.add_argument("--dry-run", action="store_true", help="コマンドを表示するだけで実行しない")
    return parser.parse_args(argv)


def load_manifest(manifest_path: Path) -> tuple[dict[str, Any], Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tiles = manifest.get("tiles")
    if not tiles:
        raise RuntimeError(f"manifest に tiles がありません: {manifest_path}")
    return manifest, manifest_path.parent


def resolve_tile_path(manifest_dir: Path, tile: dict[str, Any]) -> Path:
    if tile.get("path"):
        path = Path(tile["path"]).expanduser()
        if path.exists():
            return path.resolve()
    candidate = manifest_dir / tile["filename"]
    if candidate.exists():
        return candidate.resolve()
    raise RuntimeError(f"タイル画像が見つかりません: {tile.get('filename')}")


def build_tile_context(manifest: dict[str, Any], tile_paths: list[Path]) -> str:
    extraction = manifest.get("extraction", {})
    lines = [
        "## 入力形式の補足",
        "",
        "このリクエストでは、同じ動画範囲から作った複数のフレームタイル画像を時系列順に渡しています。",
        f"範囲: {extraction.get('start_sec', '?')}s - {extraction.get('end_sec', '?')}s",
        f"fps: {extraction.get('fps', '?')}",
        f"タイル数: {len(tile_paths)}",
        "",
        "各タイルの対応:",
    ]

    for tile, tile_path in zip(manifest["tiles"], tile_paths, strict=True):
        lines.append(
            f"- Tile {tile['tile_index']}: {tile_path.name} "
            f"(t={tile['start_timestamp_sec']:.1f}s-{tile['end_timestamp_sec']:.1f}s, "
            f"frames {tile['start_frame']}-{tile['end_frame']})"
        )

    lines.extend(
        [
            "",
            "各セルには `F<番号> t=<秒>s` のラベルが付いています。`<秒>` は動画内の絶対時刻です。",
            "複数タイルにまたがる出来事は、タイルを跨いで統合して記述してください。",
            "",
        ]
    )
    return "\n".join(lines)


def resolve_aitool(command: str | None) -> str:
    if command:
        return command
    found = shutil.which("aitool")
    if found:
        return found
    raise RuntimeError(
        "aitool が見つかりません。`uv tool install git+https://github.com/Nu424/aitool-iroiro.git` などで導入してください"
    )


def build_command(
    aitool_path: str,
    model: str,
    prompt_text: str,
    tile_paths: list[Path],
    output_path: Path,
    api_key: str | None,
) -> list[str]:
    command = [
        aitool_path,
        "recognize-image",
        "--model",
        model,
        "--text",
        prompt_text,
        "--output",
        str(output_path),
    ]
    if api_key:
        command.extend(["--api-key", api_key])
    for tile_path in tile_paths:
        command.extend(["--image", str(tile_path)])
    return command


def collect_manifest_paths(options: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if options.summary:
        summary_path = Path(options.summary).expanduser().resolve()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for result in summary.get("results", []):
            manifest_path = result.get("manifest_path")
            if manifest_path:
                paths.append(Path(manifest_path).expanduser())
    if options.manifest:
        paths.extend(Path(manifest).expanduser() for manifest in options.manifest)
    if not paths:
        raise RuntimeError("--manifest または --summary を指定してください")

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def derive_basename(manifest_path: Path) -> str:
    # multi-tile は <range_dir>/manifest.json、single-tile は <stem>.json
    if manifest_path.stem == "manifest":
        return manifest_path.parent.name
    return manifest_path.stem


def resolve_output_path(manifest_path: Path, options: argparse.Namespace, multiple: bool) -> Path:
    if options.output_dir:
        base = derive_basename(manifest_path)
        return (Path(options.output_dir).expanduser() / f"{base}_analysis.txt").resolve()
    if options.output:
        if multiple:
            raise RuntimeError(
                "複数 manifest では --output は使えません。--output-dir を使うか省略してください"
            )
        return Path(options.output).expanduser().resolve()
    base = derive_basename(manifest_path)
    if manifest_path.stem == "manifest":
        return (manifest_path.parent.parent / f"{base}_analysis.txt").resolve()
    return (manifest_path.parent / f"{base}_analysis.txt").resolve()


def analyze_one(
    manifest_path: Path,
    options: argparse.Namespace,
    prompt_body: str,
    aitool_path: str,
    output_path: Path,
) -> None:
    manifest, manifest_dir = load_manifest(manifest_path)
    tile_paths = [resolve_tile_path(manifest_dir, tile) for tile in manifest["tiles"]]
    prompt_text = f"{prompt_body}\n\n{build_tile_context(manifest, tile_paths)}"

    command = build_command(
        aitool_path,
        options.model,
        prompt_text,
        tile_paths,
        output_path,
        options.api_key,
    )

    print(f"manifest: {manifest_path}")
    print(f"  タイル数: {len(tile_paths)}")
    print(f"  出力:     {output_path}")
    if options.dry_run:
        print("  コマンド: " + " ".join(command))
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"aitool recognize-image に失敗しました (exit {completed.returncode}): {manifest_path}"
        )


def main(argv: list[str]) -> int:
    options = parse_args(argv)
    prompt_path = Path(options.prompt).expanduser().resolve()
    prompt_body = prompt_path.read_text(encoding="utf-8").strip()

    manifest_paths = collect_manifest_paths(options)
    multiple = len(manifest_paths) > 1
    aitool_path = resolve_aitool(options.aitool)

    print(f"manifest 件数: {len(manifest_paths)}")
    failures: list[tuple[Path, str]] = []
    for manifest_path in manifest_paths:
        output_path = resolve_output_path(manifest_path, options, multiple)
        try:
            analyze_one(manifest_path, options, prompt_body, aitool_path, output_path)
        except Exception as error:  # noqa: BLE001 - まとめて報告するため継続
            print(f"  失敗: {error}", file=sys.stderr)
            failures.append((manifest_path, str(error)))

    if failures:
        raise RuntimeError(f"{len(failures)}/{len(manifest_paths)} 件の解析に失敗しました")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
