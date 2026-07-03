#!/usr/bin/env python
"""タイル manifest を aitool recognize-image で解析するCLI。

1つの manifest = 原則1回の API 呼び出し（含まれる全タイルをまとめて渡す）。
タイル数が多い場合は時系列順にチャンク分割して複数回呼び出す（--max-tiles-per-call）。
複数 manifest をまとめて／並列に処理できる（--jobs）。

単一 manifest:
  python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py \
    --manifest output/agentic_sessions/example/overview/full/manifest.json \
    --prompt .agents/skills/agentic-video-analysis-skill/prompts/overview.txt \
    --output output/agentic_sessions/example/overview/full_analysis.txt

複数 manifest（タイル化 summary から自動列挙）:
  python .agents/skills/agentic-video-analysis-skill/scripts/analyze_tile_manifest.py \
    --summary output/agentic_sessions/example/candidates/batch_summary.json \
    --prompt .agents/skills/agentic-video-analysis-skill/prompts/detail.txt --jobs 4
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from common import read_json, write_json

DEFAULT_OBJECTIVE = "動画の見どころ候補の抽出"
OBJECTIVE_PLACEHOLDER = "{{OBJECTIVE}}"
DEFAULT_MAX_TILES_PER_CALL = 8

_print_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


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
        help="複数 manifest の出力をまとめるディレクトリ。各 manifest を <名前>_analysis.* で出力",
    )
    parser.add_argument(
        "--objective",
        default=None,
        help=f"解析の目的（テキスト or ファイルパス）。プロンプト内の {OBJECTIVE_PLACEHOLDER} を置換。既定: {DEFAULT_OBJECTIVE}",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="追加コンテキスト（テキスト or ファイルパス）。プロンプト末尾に付加する",
    )
    parser.add_argument(
        "--expect-json",
        action="store_true",
        help="出力をJSONとして検証し、失敗時は1回だけリトライする。整形JSONを .json に保存",
    )
    parser.add_argument(
        "--max-tiles-per-call",
        type=int,
        default=DEFAULT_MAX_TILES_PER_CALL,
        help=f"1回の呼び出しに渡すタイル数の上限（既定: {DEFAULT_MAX_TILES_PER_CALL}）。超過分は時系列で分割",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="複数 manifest を並列解析するワーカー数（既定: 1）",
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


# --- 入力テキスト（目的・コンテキスト）の解決 ---------------------------------


def resolve_text_arg(value: str | None) -> str | None:
    """値がファイルパスとして存在すればその中身を、そうでなければ値そのものを返す。"""
    if value is None:
        return None
    candidate = Path(value).expanduser()
    try:
        if candidate.exists() and candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return value


# --- manifest 読み込みとタイル解決 --------------------------------------------


def load_manifest(manifest_path: Path) -> tuple[dict[str, Any], Path]:
    manifest = read_json(manifest_path)
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


# --- プロンプト組み立て -------------------------------------------------------


def build_tile_context(
    manifest: dict[str, Any],
    tiles: list[dict[str, Any]],
    tile_paths: list[Path],
    part_info: tuple[int, int, float, float] | None = None,
) -> str:
    """タイル画像の読み方（全プロンプト共通）と、このリクエストのタイル対応を返す。"""
    extraction = manifest.get("extraction", {})
    lines = [
        "## タイル画像の読み方",
        "",
        "- 各タイルは動画から抜いたフレームを格子状に並べた画像。セルは **左→右、上→下** の順で時系列。",
        "- タイルが複数あるときは Tile 0 → Tile 1 → … の順で時刻が連続する。",
        "- 各セルには `F<番号> t=<秒>s` のラベルが付く。`<秒>` は動画内の**絶対時刻**。",
        "- 隣接セルの内容を混同しないこと。まず各タイルを1枚ずつ走査し、その後で全体を統合する。",
        "- 複数タイル・複数セルにまたがる出来事は、統合して1件として記述する。",
        "",
        "## このリクエストのタイル",
        "",
        f"範囲: {extraction.get('start_sec', '?')}s - {extraction.get('end_sec', '?')}s"
        f" / fps: {extraction.get('fps', '?')} / タイル数: {len(tile_paths)}",
    ]

    if part_info is not None:
        part_index, part_count, part_start, part_end = part_info
        lines.append(
            f"※ これは分割解析のパート {part_index + 1}/{part_count}"
            f"（この呼び出しが担当するのは {part_start:.1f}s - {part_end:.1f}s）。"
            "このパートで確認できる範囲だけを対象にすること。"
        )

    lines.append("")
    lines.append("各タイルの対応:")
    for tile, tile_path in zip(tiles, tile_paths, strict=True):
        lines.append(
            f"- Tile {tile['tile_index']}: {tile_path.name} "
            f"(t={tile['start_timestamp_sec']:.1f}s-{tile['end_timestamp_sec']:.1f}s, "
            f"frames {tile['start_frame']}-{tile['end_frame']})"
        )
    lines.append("")
    return "\n".join(lines)


def apply_objective(prompt_body: str, objective: str) -> str:
    if OBJECTIVE_PLACEHOLDER in prompt_body:
        return prompt_body.replace(OBJECTIVE_PLACEHOLDER, objective)
    return prompt_body


def build_hypothesis_block(note: str) -> str:
    return (
        "## 事前の仮説（全体把握での観察）\n"
        f"{note}\n\n"
        "この仮説を映像上の根拠で確認・反証し、hypothesis_verdict に回答すること。\n"
        "仮説に引きずられず、見えている事実を優先すること。"
    )


def assemble_prompt(
    prompt_body: str,
    objective: str,
    tile_context: str,
    context_text: str | None,
    note: str | None,
    retry_hint: bool = False,
) -> str:
    parts = [apply_objective(prompt_body, objective), tile_context]
    if note:
        parts.append(build_hypothesis_block(note))
    if context_text:
        parts.append(f"## 追加コンテキスト\n{context_text}")
    if retry_hint:
        parts.append("前回の出力はJSONとして不正だった。コードフェンス付きJSONのみを出力せよ。")
    return "\n\n".join(part for part in parts if part)


# --- aitool 実行 --------------------------------------------------------------


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


# --- JSON 抽出 ----------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> Any:
    """コードフェンスを剥がして json.loads する。失敗時は ValueError を送出。"""
    import json

    stripped = text.strip()
    match = _FENCE_RE.search(stripped)
    payload = match.group(1) if match else stripped
    return json.loads(payload)


# --- 出力パスの解決 -----------------------------------------------------------


def derive_basename(manifest_path: Path) -> str:
    # multi-tile は <range_dir>/manifest.json、single-tile は <stem>.json
    if manifest_path.stem == "manifest":
        return manifest_path.parent.name
    return manifest_path.stem


def resolve_output_base(manifest_path: Path, options: argparse.Namespace, multiple: bool) -> Path:
    """拡張子を含まない出力ベースパス（.../<name>_analysis）を返す。"""
    if options.output_dir:
        base = derive_basename(manifest_path)
        return (Path(options.output_dir).expanduser() / f"{base}_analysis").resolve()
    if options.output:
        if multiple:
            raise RuntimeError(
                "複数 manifest では --output は使えません。--output-dir を使うか省略してください"
            )
        out = Path(options.output).expanduser()
        return out.with_suffix("").resolve()
    base = derive_basename(manifest_path)
    if manifest_path.stem == "manifest":
        return (manifest_path.parent.parent / f"{base}_analysis").resolve()
    return (manifest_path.parent / f"{base}_analysis").resolve()


# --- タイルのチャンク分割（1タイルオーバーラップ） ---------------------------


def chunk_with_overlap(items: list[Any], max_per: int) -> list[list[Any]]:
    if max_per < 1:
        raise RuntimeError("--max-tiles-per-call は 1 以上を指定してください")
    if len(items) <= max_per:
        return [items]
    step = max(1, max_per - 1)  # 1タイルオーバーラップ
    chunks: list[list[Any]] = []
    index = 0
    while index < len(items):
        chunks.append(items[index : index + max_per])
        if index + max_per >= len(items):
            break
        index += step
    return chunks


# --- 分割JSONの機械的統合 -----------------------------------------------------


def merge_part_results(parsed_parts: list[Any]) -> dict[str, Any]:
    """各パートのJSON(dict)を機械的に統合する。

    - list 値のキーは連結（overview: candidates, detail: events など）
    - スカラ値のキーは `<key>_parts` に配列で保持（summary, hypothesis_verdict など）
    重複しうる旨は SKILL.md で説明。判断はエージェントに委ねる。
    """
    merged: dict[str, Any] = {}
    for part in parsed_parts:
        if not isinstance(part, dict):
            merged.setdefault("_unstructured_parts", []).append(part)
            continue
        for key, value in part.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            else:
                merged.setdefault(f"{key}_parts", []).append(value)
    merged["_merged_from_parts"] = len(parsed_parts)
    return merged


# --- 1 manifest の解析 --------------------------------------------------------


def _run_aitool(command: list[str], manifest_path: Path) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"aitool recognize-image に失敗しました (exit {completed.returncode}): {manifest_path}"
        )


def _finalize_output(
    raw_text_path: Path,
    base: Path,
    part_suffix: str,
    expect_json: bool,
) -> tuple[Any | None, bool]:
    """raw 出力を検証・整形保存する。(parsed, json_ok) を返す。expect_json でなければ (None, True)。"""
    if not expect_json:
        return None, True
    raw_text = raw_text_path.read_text(encoding="utf-8")
    try:
        parsed = extract_json(raw_text)
    except (ValueError, TypeError):
        return None, False
    json_path = base.with_name(f"{base.name}{part_suffix}.json")
    write_json(json_path, parsed)
    return parsed, True


def analyze_one(
    manifest_path: Path,
    options: argparse.Namespace,
    prompt_body: str,
    objective: str,
    context_text: str | None,
    note: str | None,
    aitool_path: str,
    base: Path,
) -> dict[str, Any]:
    manifest, manifest_dir = load_manifest(manifest_path)
    tiles = manifest["tiles"]
    tile_paths = [resolve_tile_path(manifest_dir, tile) for tile in tiles]

    paired = list(zip(tiles, tile_paths, strict=True))
    chunks = chunk_with_overlap(paired, options.max_tiles_per_call)
    part_count = len(chunks)
    base.parent.mkdir(parents=True, exist_ok=True)

    log(f"[開始] {manifest_path}  タイル{len(tiles)}枚 / {part_count}パート -> {base}.*")

    json_failures = 0
    parsed_parts: list[Any] = []
    raw_texts: list[str] = []

    for part_index, chunk in enumerate(chunks):
        chunk_tiles = [item[0] for item in chunk]
        chunk_paths = [item[1] for item in chunk]
        part_info = (
            (part_index, part_count, chunk_tiles[0]["start_timestamp_sec"], chunk_tiles[-1]["end_timestamp_sec"])
            if part_count > 1
            else None
        )
        tile_context = build_tile_context(manifest, chunk_tiles, chunk_paths, part_info)

        # 出力先: 単一パートは base 直下、複数パートは _partNN
        part_suffix = "" if part_count == 1 else f"_part{part_index:02d}"
        ext = ".raw.txt" if options.expect_json else ".txt"
        raw_text_path = base.with_name(f"{base.name}{part_suffix}{ext}")

        prompt_text = assemble_prompt(prompt_body, objective, tile_context, context_text, note)
        command = build_command(
            aitool_path, options.model, prompt_text, chunk_paths, raw_text_path, options.api_key
        )

        if options.dry_run:
            log(f"  [dry-run] part {part_index + 1}/{part_count}: " + " ".join(command))
            continue

        _run_aitool(command, manifest_path)
        parsed, json_ok = _finalize_output(raw_text_path, base, part_suffix, options.expect_json)

        if options.expect_json and not json_ok:
            # 1回だけリトライ
            log(f"  [警告] part {part_index + 1}: JSON不正。リトライします")
            retry_prompt = assemble_prompt(
                prompt_body, objective, tile_context, context_text, note, retry_hint=True
            )
            retry_command = build_command(
                aitool_path, options.model, retry_prompt, chunk_paths, raw_text_path, options.api_key
            )
            _run_aitool(retry_command, manifest_path)
            parsed, json_ok = _finalize_output(raw_text_path, base, part_suffix, options.expect_json)
            if not json_ok:
                json_failures += 1
                log(f"  [警告] part {part_index + 1}: リトライも失敗。生テキストのみ保存し継続")

        if options.expect_json and parsed is not None:
            parsed_parts.append(parsed)
        if not options.dry_run:
            raw_texts.append(raw_text_path.read_text(encoding="utf-8"))

    if options.dry_run:
        return {"manifest": str(manifest_path), "json_failures": 0, "parts": part_count}

    # 複数パートの統合出力
    if part_count > 1:
        if options.expect_json and parsed_parts:
            write_json(base.with_suffix(".json"), merge_part_results(parsed_parts))
        elif not options.expect_json:
            combined = "\n\n---\n\n".join(raw_texts)
            base.with_suffix(".txt").write_text(combined, encoding="utf-8")

    log(f"[完了] {manifest_path}" + (f"  JSON失敗{json_failures}件" if json_failures else ""))
    return {"manifest": str(manifest_path), "json_failures": json_failures, "parts": part_count}


# --- manifest 列挙と note の対応付け -------------------------------------------


def collect_manifests(options: argparse.Namespace) -> list[tuple[Path, str | None]]:
    """(manifest_path, note) のリストを返す。summary の results[].note を対応付ける。"""
    pairs: list[tuple[Path, str | None]] = []
    if options.summary:
        summary_path = Path(options.summary).expanduser().resolve()
        summary = read_json(summary_path)
        for result in summary.get("results", []):
            manifest_path = result.get("manifest_path")
            if manifest_path:
                pairs.append((Path(manifest_path).expanduser(), result.get("note")))
    if options.manifest:
        pairs.extend((Path(manifest).expanduser(), None) for manifest in options.manifest)
    if not pairs:
        raise RuntimeError("--manifest または --summary を指定してください")

    seen: set[Path] = set()
    unique: list[tuple[Path, str | None]] = []
    for path, note in pairs:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((resolved, note))
    return unique


def main(argv: list[str]) -> int:
    options = parse_args(argv)
    if options.jobs < 1:
        raise RuntimeError("--jobs は 1 以上を指定してください")

    prompt_path = Path(options.prompt).expanduser().resolve()
    prompt_body = prompt_path.read_text(encoding="utf-8").strip()
    objective = resolve_text_arg(options.objective) or DEFAULT_OBJECTIVE
    context_text = resolve_text_arg(options.context)

    manifests = collect_manifests(options)
    multiple = len(manifests) > 1
    aitool_path = resolve_aitool(options.aitool) if not options.dry_run else (options.aitool or "aitool")

    # 出力先ベースを事前解決し、主要出力パスの衝突を検査する
    jobs: list[tuple[Path, str | None, Path]] = []
    primary_outputs: dict[Path, Path] = {}
    for manifest_path, note in manifests:
        base = resolve_output_base(manifest_path, options, multiple)
        if base in primary_outputs:
            raise RuntimeError(
                f"出力先が衝突します: {base}.* （{manifest_path} と {primary_outputs[base]}）"
            )
        primary_outputs[base] = manifest_path
        jobs.append((manifest_path, note, base))

    log(f"manifest 件数: {len(jobs)} / jobs={options.jobs} / max-tiles-per-call={options.max_tiles_per_call}")

    failures: list[tuple[Path, str]] = []
    json_failure_total = 0

    def worker(manifest_path: Path, note: str | None, base: Path) -> dict[str, Any]:
        return analyze_one(
            manifest_path, options, prompt_body, objective, context_text, note, aitool_path, base
        )

    if options.jobs == 1 or len(jobs) == 1:
        for manifest_path, note, base in jobs:
            try:
                report = worker(manifest_path, note, base)
                json_failure_total += report["json_failures"]
            except Exception as error:  # noqa: BLE001 - まとめて報告するため継続
                log(f"  失敗: {error}")
                failures.append((manifest_path, str(error)))
    else:
        with ThreadPoolExecutor(max_workers=options.jobs) as executor:
            future_map = {
                executor.submit(worker, manifest_path, note, base): manifest_path
                for manifest_path, note, base in jobs
            }
            for future in as_completed(future_map):
                manifest_path = future_map[future]
                try:
                    report = future.result()
                    json_failure_total += report["json_failures"]
                except Exception as error:  # noqa: BLE001
                    log(f"  失敗: {error}")
                    failures.append((manifest_path, str(error)))

    if json_failure_total:
        log(f"JSON検証に失敗したパート: {json_failure_total} 件（生テキストは保存済み）")
    if failures:
        raise RuntimeError(f"{len(failures)}/{len(jobs)} 件の解析に失敗しました")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
