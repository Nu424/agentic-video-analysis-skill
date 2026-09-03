#!/usr/bin/env python
"""範囲ごとの解析結果を1本のタイムラインに統合する（機械統合 → LLM統合 → final.md）。

段階は4つ。

1. **収集**: セッション配下の `<label>_validated.json`（無ければ `<label>_analysis.json`）から
   events を集め、各項目に `sources: [<range label>]` を付ける。
   overview / zooms / refinements / audio は対象外。
2. **機械統合** (`merge_events`): 時系列に並べ、時間重なり率 >= `overlap_threshold`（短い方基準）
   かつタイトル類似度 >= `title_similarity`（`difflib.SequenceMatcher`）の項目を1件にまとめる。
   統合するのは**異なる範囲**に属する項目同士だけ（目的は範囲境界をまたぐ重複の解消なので、
   同じ範囲が挙げた2件は別の出来事として残す）。
   秒数は包含、confidence は低い方、`sources` / `flags` は和集合、`evidence` は連結重複除去、
   `summary` は長い方。結果は `merge/timeline_mechanical.json` に必ず残す（監査用）。
3. **LLM統合** (`llm_merge`、既定 ON): `prompts/merge.txt` に機械統合の結果を渡して
   `overview` と `timeline` を得る。出力はそのまま採らず `reconcile_llm_timeline` で
   機械統合の結果と 1 対 1 に照合し、欠けた `sources` / `flags` / `evidence` を補い、
   `confidence` の格上げを打ち消す。LLM が落とした項目は `llm_dropped` を付けて timeline に戻す
   （出来事を落とさない）。差分は `validation_report.json` の `merge_diff` に残す。
4. **出力**: `merge/timeline.json`、`--final-md` なら `final.md`。

このほかに、複数回実行の**和集合**（`union_timelines`。各項目に `runs`）と、
音声解析の取り込み（`attach_audio`。映像と重ならない項目に `audio_unconfirmed`）がある。
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from avs.backends import LLMRequest, get_backend
from avs.common import read_json, read_text_utf8, write_json
from avs.prompts import (
    DEFAULT_OBJECTIVE,
    MERGE_SCHEMA,
    api_schema,
    assemble_prompt,
    build_importance_block,
    load_domain,
    resolve_text_arg,
)
from avs.session import Session, append_usage_line, build_call_meta

DEFAULT_OVERLAP_THRESHOLD = 0.5
DEFAULT_TITLE_SIMILARITY = 0.6
DEFAULT_LLM_CHUNK = 80
DEFAULT_BACKEND = "openrouter"
DEFAULT_IMPORTANCE = "medium"

TIMELINE_FILENAME = "timeline.json"
MECHANICAL_FILENAME = "timeline_mechanical.json"
UNION_FILENAME = "timeline_union.json"
REPORT_FILENAME = "validation_report.json"
FINAL_MD_FILENAME = "final.md"
MERGE_DIR_NAME = "merge"

# merge の監査記録のファイル名。`merge_01.prompt.txt` / `merge_01.meta.json` のように
# チャンク番号（1 始まり）を付ける
MERGE_RECORD_PREFIX = "merge"
MERGE_STEP = "merge"

# 収集の対象外（overview / ズーム / 精密確認 / 音声 / 統合結果は detail ではない）
EXCLUDED_DIR_NAMES = {"overview", "zooms", "refinements", "audio", "merge", "uploads", "notes"}
EXCLUDED_LABEL_PREFIXES = ("overview", "chapters", "audio", "timeline")

CONFIDENCE_ORDER = ("high", "medium", "low")
IMPORTANCE_ORDER = ("high", "medium", "low")

# 時間が「動いた」と見なす差（秒）
TIME_MOVE_EPSILON = 0.05

# LLM 統合の照合で付けるフラグ
LLM_UNMATCHED_FLAG = "llm_unmatched"  # 機械統合に対応が無い LLM 項目（新規に作られた項目）
LLM_DROPPED_FLAG = "llm_dropped"  # LLM が落としたので機械統合から復元した項目

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def log(message: str) -> None:
    print(message, flush=True)


# --- JSON 抽出（analysis の内部実装に依存しないよう独立に持つ） --------------------


def extract_json(text: str) -> Any:
    """コードフェンスを剥がして json.loads する。失敗時は ValueError。"""
    stripped = text.strip()
    match = _FENCE_RE.search(stripped)
    payload = match.group(1) if match else stripped
    return json.loads(payload)


# --- 重複判定 --------------------------------------------------------------------


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def overlap_ratio(a: dict[str, Any], b: dict[str, Any]) -> float:
    """時間の重なり率（短い方の長さを基準にする）。長さ0の項目は接触で1.0。"""
    a_start, a_end = _as_float(a.get("start_sec")), _as_float(a.get("end_sec"))
    b_start, b_end = _as_float(b.get("start_sec")), _as_float(b.get("end_sec"))
    if a_end < a_start:
        a_start, a_end = a_end, a_start
    if b_end < b_start:
        b_start, b_end = b_end, b_start

    overlap = min(a_end, b_end) - max(a_start, b_start)
    if overlap < 0:
        return 0.0
    shortest = min(a_end - a_start, b_end - b_start)
    if shortest <= 0:
        return 1.0 if overlap >= 0 else 0.0
    return overlap / shortest


def title_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """タイトルの類似度（`difflib.SequenceMatcher`）。"""
    return SequenceMatcher(None, str(a.get("title") or ""), str(b.get("title") or "")).ratio()


def is_duplicate(
    a: dict[str, Any],
    b: dict[str, Any],
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    title_threshold: float = DEFAULT_TITLE_SIMILARITY,
) -> bool:
    """同一の出来事とみなすか（時間の重なり率とタイトル類似度の両方を満たすとき）。"""
    return (
        overlap_ratio(a, b) >= overlap_threshold
        and title_similarity(a, b) >= title_threshold
    )


# --- 項目の正規化と統合 -----------------------------------------------------------


def _dedupe(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _lower_confidence(a: Any, b: Any) -> Any:
    if a not in CONFIDENCE_ORDER:
        return b
    if b not in CONFIDENCE_ORDER:
        return a
    return a if CONFIDENCE_ORDER.index(a) >= CONFIDENCE_ORDER.index(b) else b


def normalize_event(event: dict[str, Any], source: str | None = None) -> dict[str, Any]:
    """detail の event を timeline の項目（MERGE_SCHEMA の timeline[]）に寄せる。"""
    evidence = event.get("evidence")
    if not evidence:
        evidence = event.get("visual")
    sources = [str(item) for item in (event.get("sources") or []) if item]
    if source and source not in sources:
        sources.append(source)

    item: dict[str, Any] = {
        "start_sec": _as_float(event.get("start_sec")),
        "end_sec": _as_float(event.get("end_sec")),
        "title": str(event.get("title") or ""),
        "summary": str(event.get("summary") or ""),
        "importance": event.get("importance") or DEFAULT_IMPORTANCE,
        "confidence": event.get("confidence_adjusted") or event.get("confidence") or "medium",
        "evidence": [str(value) for value in (evidence or []) if isinstance(value, str)],
        "sources": sources,
        "flags": [str(flag) for flag in (event.get("flags") or []) if flag],
    }
    for key in ("runs", "source", "kind"):
        if event.get(key):
            item[key] = event[key]
    return item


def merge_pair(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """同一とみなした2項目を1件にまとめる。"""
    merged = dict(a)
    merged["start_sec"] = min(_as_float(a.get("start_sec")), _as_float(b.get("start_sec")))
    merged["end_sec"] = max(_as_float(a.get("end_sec")), _as_float(b.get("end_sec")))
    merged["confidence"] = _lower_confidence(a.get("confidence"), b.get("confidence"))
    merged["sources"] = _dedupe([*(a.get("sources") or []), *(b.get("sources") or [])])
    merged["flags"] = _dedupe([*(a.get("flags") or []), *(b.get("flags") or [])])
    merged["evidence"] = _dedupe([*(a.get("evidence") or []), *(b.get("evidence") or [])])
    a_summary = str(a.get("summary") or "")
    b_summary = str(b.get("summary") or "")
    merged["summary"] = a_summary if len(a_summary) >= len(b_summary) else b_summary
    if a.get("runs") or b.get("runs"):
        merged["runs"] = _dedupe([*(a.get("runs") or []), *(b.get("runs") or [])])
    # importance は「高い方」を残す（統合で重要度が下がるのを避ける）
    for key, order in (("importance", IMPORTANCE_ORDER),):
        a_value, b_value = a.get(key), b.get(key)
        if a_value in order and b_value in order:
            merged[key] = a_value if order.index(a_value) <= order.index(b_value) else b_value
    return merged


def _origin_values(item: dict[str, Any], key: str) -> set[str]:
    return {str(value) for value in (item.get(key) or []) if value}


def has_distinct_origin(a: dict[str, Any], b: dict[str, Any], origin_key: str) -> bool:
    """2項目の出所（`sources` = 範囲ラベル / `runs` = 実行名）が重ならないか。

    出所が同じ項目同士は統合しない。機械統合の目的は**範囲境界をまたぐ重複**の解消なので、
    同じ範囲の解析が挙げた 2 件は、時間が重なってタイトルが似ていても別の出来事として扱う
    （同じ範囲の中で似た出来事が続くのは普通にあるし、そこを潰すと件数が過少になる。§4.6-1）。
    和集合（`union_timelines`）では同じ理由で `runs` を見る。

    出所が両方とも空のときは（判断材料が無いので）統合を許す。
    """
    return not (_origin_values(a, origin_key) & _origin_values(b, origin_key))


def merge_events(
    events: list[dict[str, Any]],
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    title_similarity_threshold: float = DEFAULT_TITLE_SIMILARITY,
    origin_key: str = "sources",
) -> list[dict[str, Any]]:
    """時系列に並べ、**出所の異なる**重複項目を機械的に1件へ統合する。

    `origin_key` は「同じ出所か」を判断するキー。機械統合では `sources`（範囲ラベル）、
    和集合では `runs`（実行名）を使う。統合先は既に確定した項目を後ろから探す
    （直前の項目が同じ出所で統合できないとき、その 1 つ前と統合できることがある）。
    """
    items = [normalize_event(event) for event in events if isinstance(event, dict)]
    items.sort(key=lambda item: (_as_float(item.get("start_sec")), _as_float(item.get("end_sec"))))

    merged: list[dict[str, Any]] = []
    for item in items:
        target: int | None = None
        for index in range(len(merged) - 1, -1, -1):
            candidate = merged[index]
            if not has_distinct_origin(candidate, item, origin_key):
                continue
            if is_duplicate(candidate, item, overlap_threshold, title_similarity_threshold):
                target = index
                break
        if target is None:
            merged.append(item)
            continue
        merged[target] = merge_pair(merged[target], item)
    return merged


# --- 入力の収集 ------------------------------------------------------------------


def label_from_path(path: Path) -> str:
    """`<label>_validated.json` / `<label>_analysis.json` からラベルを取り出す。"""
    stem = path.stem
    for suffix in ("_validated", "_analysis"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


@dataclass
class Collected:
    """収集結果（events と、その出所）。"""

    events: list[dict[str, Any]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    hypothesis_rejected: list[str] = field(default_factory=list)


def _is_excluded(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    if any(part in EXCLUDED_DIR_NAMES for part in relative.parts[:-1]):
        return True
    label = label_from_path(path).lower()
    return label.startswith(EXCLUDED_LABEL_PREFIXES)


def find_analysis_files(session_dir: Path) -> list[Path]:
    """セッション配下の detail 解析結果を列挙する（`_validated.json` を優先）。

    `<session>/ranges/` があればその配下だけを見る。無ければセッション全体を走査し、
    overview / zooms / refinements / audio / merge を除外する。
    """
    root = session_dir / "ranges"
    if not root.exists():
        root = session_dir

    by_label: dict[str, Path] = {}
    for pattern, preferred in (("*_analysis.json", False), ("*_validated.json", True)):
        for path in sorted(root.rglob(pattern)):
            if _is_excluded(path, root):
                continue
            label = label_from_path(path)
            if preferred or label not in by_label:
                by_label[label] = path
    return [by_label[label] for label in sorted(by_label)]


def load_events_from_file(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """1ファイルから events を読み、`sources` にラベルを付ける。(events, 仮説反証か) を返す。"""
    data = read_json(path)
    if not isinstance(data, dict):
        raise RuntimeError(f"解析結果がオブジェクトではありません: {path}")
    label = label_from_path(path)
    events = [
        normalize_event(event, source=label)
        for event in (data.get("events") or [])
        if isinstance(event, dict)
    ]
    rejected = data.get("hypothesis_verdict") == "rejected" or "hypothesis_rejected" in (
        data.get("flags") or []
    )
    return events, bool(rejected)


def collect_events(
    session: str | Path | None = None,
    inputs: list[str] | None = None,
) -> Collected:
    """`--session` 配下、または `--inputs` で指定されたファイルから events を集める。"""
    paths: list[Path] = []
    if inputs:
        for value in inputs:
            path = Path(value).expanduser()
            if not path.exists():
                raise RuntimeError(f"入力ファイルが見つかりません: {path}")
            paths.append(path)
    elif session is not None:
        session_dir = Path(session).expanduser().resolve()
        if not session_dir.exists():
            raise RuntimeError(f"セッションディレクトリが見つかりません: {session_dir}")
        paths = find_analysis_files(session_dir)
    else:
        raise RuntimeError("--session または --inputs を指定してください")

    collected = Collected()
    for path in paths:
        events, rejected = load_events_from_file(path)
        collected.events.extend(events)
        collected.files.append(str(path))
        if rejected:
            collected.hypothesis_rejected.append(label_from_path(path))
    return collected


# --- タイムライン文書 -------------------------------------------------------------


def build_timeline_doc(
    timeline: list[dict[str, Any]],
    overview: str = "",
    collected: Collected | None = None,
    **extra: Any,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overview": overview,
        "timeline": timeline,
    }
    if collected is not None:
        doc["source_files"] = collected.files
        doc["hypothesis_rejected"] = collected.hypothesis_rejected
    doc.update(extra)
    return doc


def load_timeline(path: Path) -> dict[str, Any]:
    """`timeline.json`（dict）または timeline 配列そのものを読む。"""
    data = read_json(path)
    if isinstance(data, list):
        return {"overview": "", "timeline": data}
    if not isinstance(data, dict):
        raise RuntimeError(f"タイムラインとして読めません: {path}")
    if "timeline" not in data:
        raise RuntimeError(f"timeline キーがありません: {path}")
    return data


# --- LLM 統合 --------------------------------------------------------------------


def default_prompt_path() -> Path:
    """スキル同梱の `prompts/merge.txt`。"""
    return Path(__file__).resolve().parents[2] / "prompts" / "merge.txt"


def chunk_items(items: list[dict[str, Any]], chunk_size: int) -> list[list[dict[str, Any]]]:
    """時間順のまま `chunk_size` 件ずつに分割する。"""
    if chunk_size < 1:
        raise RuntimeError("--llm-chunk は 1 以上を指定してください")
    if len(items) <= chunk_size:
        return [items]
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def build_merge_prompt(
    items: list[dict[str, Any]],
    objective: str,
    domain: dict[str, Any] | None,
    prompt_body: str,
    retry_hint: bool = False,
) -> str:
    """`merge.txt` + 機械統合結果（JSON）+ objective + domain でプロンプトを組む。"""
    context_text = json.dumps({"timeline": items}, ensure_ascii=False, indent=2)
    prompt = assemble_prompt(
        prompt_body,
        objective,
        tile_context="",
        context_text=context_text,
        note=None,
        retry_hint=retry_hint,
        domain=domain,
        schema=MERGE_SCHEMA,
    )
    importance_block = build_importance_block(domain)
    return f"{prompt}\n\n{importance_block}" if importance_block else prompt




def _write_merge_call_records(
    records_dir: Path | None,
    session_dir: Path | None,
    chunk_label: str,
    prompts: list[str],
    responses: list[Any],
) -> None:
    """1 チャンク分の監査記録（prompt.txt / retry.prompt.txt / meta.json / usage.jsonl）を書く。

    形は `avs.analysis` の呼び出し記録と同じ（`build_call_meta`）。
    ファイル名は `merge_<chunk>.*`。リトライした分の usage は合算して 1 件にする。
    """
    name = f"{MERGE_RECORD_PREFIX}_{chunk_label}"
    if records_dir is not None:
        records_dir.mkdir(parents=True, exist_ok=True)
        (records_dir / f"{name}.prompt.txt").write_text(prompts[0], encoding="utf-8")
        for retry_index, retry_prompt in enumerate(prompts[1:], start=1):
            suffix = ".retry" if retry_index == 1 else f".retry{retry_index}"
            (records_dir / f"{name}{suffix}.prompt.txt").write_text(retry_prompt, encoding="utf-8")

    meta = build_call_meta(
        responses,
        media_desc=[],
        prompt_chars=len(prompts[-1]),
        json_retries=len(prompts) - 1,
        step=MERGE_STEP,
    )
    if records_dir is not None:
        write_json(records_dir / f"{name}.meta.json", meta)
    append_usage_line(session_dir, {"name": name, **meta})


def llm_merge(
    items: list[dict[str, Any]],
    objective: str = DEFAULT_OBJECTIVE,
    domain: dict[str, Any] | None = None,
    backend: str = DEFAULT_BACKEND,
    model: str | None = None,
    api_key: str | None = None,
    strict_json: bool = False,
    chunk_size: int = DEFAULT_LLM_CHUNK,
    prompt_path: str | Path | None = None,
    session_dir: Path | None = None,
    raw_output_path: Path | None = None,
    records_dir: Path | None = None,
) -> dict[str, Any]:
    """機械統合の結果を LLM に渡して `{"overview", "timeline"}` を得る。

    コードフェンス付きJSONを抽出し、失敗したら1回だけリトライする。
    `chunk_size` を超える件数は時間順に分割して呼び、結果を連結する。

    チャンクごとに `records_dir` へ `merge_<chunk>.prompt.txt` と `merge_<chunk>.meta.json` を書き、
    `session_dir` があれば `usage.jsonl` に 1 行追記する（リトライ分は 1 件に合算。§4.7）。

    戻り値は LLM の出力をそのまま正規化したもの。採用前に `reconcile_llm_timeline` で
    機械統合の結果と照合すること。
    """
    body_path = Path(prompt_path).expanduser() if prompt_path else default_prompt_path()
    prompt_body = read_text_utf8(body_path)
    client = get_backend(backend, model=model, api_key=api_key, strict_json=strict_json)

    chunks = chunk_items(items, chunk_size)
    overviews: list[str] = []
    timeline: list[dict[str, Any]] = []
    raw_parts: list[str] = []

    for index, chunk in enumerate(chunks):
        parsed: Any = None
        last_error: str | None = None
        prompts: list[str] = []
        responses: list[Any] = []
        for attempt in range(2):  # 本番 + JSON 失敗時の1回リトライ
            prompt = build_merge_prompt(chunk, objective, domain, prompt_body, retry_hint=attempt > 0)
            prompts.append(prompt)
            request = LLMRequest(
                prompt=prompt,
                media=[],
                json_schema=api_schema(MERGE_SCHEMA),
                model=model,
            )
            response = client.complete(request)
            responses.append(response)
            raw_parts.append(response.text)

            try:
                parsed = extract_json(response.text)
            except (ValueError, TypeError) as error:
                last_error = str(error)
                parsed = None
                continue
            break

        _write_merge_call_records(
            records_dir, session_dir, f"{index + 1:02d}", prompts, responses
        )

        if raw_output_path is not None:
            raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            raw_output_path.write_text("\n\n".join(raw_parts), encoding="utf-8")

        if not isinstance(parsed, dict):
            raise RuntimeError(
                "LLM統合の出力をJSONとして解析できませんでした"
                + (f": {last_error}" if last_error else "")
                + (f"（生出力: {raw_output_path}）" if raw_output_path else "")
            )

        overview = str(parsed.get("overview") or "").strip()
        if overview:
            overviews.append(overview)
        timeline.extend(
            normalize_event(entry) for entry in (parsed.get("timeline") or []) if isinstance(entry, dict)
        )

    timeline.sort(key=lambda item: (_as_float(item.get("start_sec")), _as_float(item.get("end_sec"))))
    return {"overview": " ".join(_dedupe(overviews)), "timeline": timeline}


# --- LLM 統合前後の差分 -----------------------------------------------------------


def _describe_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "start_sec": item.get("start_sec"),
        "end_sec": item.get("end_sec"),
        "sources": item.get("sources") or [],
    }


def pair_items(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    title_threshold: float = DEFAULT_TITLE_SIMILARITY,
) -> dict[int, int]:
    """`before[i]` に対応する `after` の index を **1 対 1** で決める（時間重なり + 題名類似）。

    先頭から貪欲に対応付け、既に使った `after` の項目は候補から外す。
    1 対多を許すと、LLM が 1 件にまとめた項目に機械統合の複数件が対応してしまい、
    「落ちた項目」が検出できなくなる。対応の付かなかった `before` は落ちた項目、
    対応の付かなかった `after` は LLM が新しく作った項目として扱う。
    """
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for index, item in enumerate(before):
        for candidate_index, candidate in enumerate(after):
            if candidate_index in used:
                continue
            if is_duplicate(item, candidate, overlap_threshold, title_threshold):
                pairs[index] = candidate_index
                used.add(candidate_index)
                break
    return pairs


def _build_diff(
    before: list[dict[str, Any]], after: list[dict[str, Any]], pairs: dict[int, int]
) -> dict[str, Any]:
    dropped = [_describe_item(item) for index, item in enumerate(before) if index not in pairs]
    matched_after = set(pairs.values())
    added = [_describe_item(item) for index, item in enumerate(after) if index not in matched_after]
    moved: list[dict[str, Any]] = []
    for index, candidate_index in pairs.items():
        item, candidate = before[index], after[candidate_index]
        start_delta = _as_float(candidate.get("start_sec")) - _as_float(item.get("start_sec"))
        end_delta = _as_float(candidate.get("end_sec")) - _as_float(item.get("end_sec"))
        if abs(start_delta) > TIME_MOVE_EPSILON or abs(end_delta) > TIME_MOVE_EPSILON:
            moved.append(
                {
                    "title": item.get("title"),
                    "before": [item.get("start_sec"), item.get("end_sec")],
                    "after": [candidate.get("start_sec"), candidate.get("end_sec")],
                }
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_before": len(before),
        "n_after": len(after),
        "dropped": dropped,
        "added": added,
        "moved": moved,
    }


def merge_diff(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    title_threshold: float = DEFAULT_TITLE_SIMILARITY,
) -> dict[str, Any]:
    """LLM 統合の前後で、消えた項目・増えた項目・時間が動いた項目を洗い出す（1 対 1 の照合）。"""
    return _build_diff(before, after, pair_items(before, after, overlap_threshold, title_threshold))


def _restore_machine_fields(llm_item: dict[str, Any], machine_item: dict[str, Any]) -> dict[str, Any]:
    """LLM 項目に、対応する機械統合項目の事実を戻す（§4.6-2 の「入力より格上げしない」の強制）。

    - `sources`: 機械側との和集合。出所（どの範囲から来たか）は監査の要なので LLM に消させない
    - `flags`: 機械側との和集合。後段バリデーションが付けた注意書きを落とさない
    - `evidence`: LLM 側が空のときだけ機械側で補う（言い換えた根拠を二重に並べない）
    - `confidence`: 機械側と LLM 側の**低い方**。LLM が格上げしていたら機械側の値に戻す
    - `start_sec` / `end_sec` / `title` / `summary` / `importance`: LLM 側を採る
      （読みやすさのための言い換えと時間の整理は LLM 統合の仕事。差分は `merge_diff` に残る）
    """
    item = dict(llm_item)
    item["sources"] = _dedupe([*(llm_item.get("sources") or []), *(machine_item.get("sources") or [])])
    item["flags"] = _dedupe([*(llm_item.get("flags") or []), *(machine_item.get("flags") or [])])
    if not item.get("evidence"):
        item["evidence"] = list(machine_item.get("evidence") or [])
    item["confidence"] = _lower_confidence(llm_item.get("confidence"), machine_item.get("confidence"))
    return item


def reconcile_llm_timeline(
    mechanical: list[dict[str, Any]],
    llm_timeline: list[dict[str, Any]],
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    title_threshold: float = DEFAULT_TITLE_SIMILARITY,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """LLM 統合の出力を機械統合の結果と照合して、採用する timeline と差分を返す。

    LLM は要約と重複整理には強いが、**出来事を落とす**・**確信度を上げる**・
    **出所や根拠を書き落とす**ことがある。そのまま採ると監査可能性と網羅率が下がるので、
    機械統合の結果を「事実の台帳」として次のように照合する（§4.6-2）:

    1. 時間重なり + 題名類似で 1 対 1 に対応付ける（`pair_items`）
    2. 対応が付いた LLM 項目は `_restore_machine_fields` で機械側の事実を戻す
    3. 対応の付かない LLM 項目は `llm_unmatched` を付けて残す（消さない。判断はエージェント）
    4. 対応の付かない機械項目は `llm_dropped` を付けて timeline に**復元**し、
       同じものを差分の `dropped` にも載せる（出来事を落とさない、が原則）

    戻り値は (timeline, merge_diff)。`merge_diff` は復元前の LLM 出力に対する差分なので、
    「LLM が何をしたか」がそのまま読める。
    """
    pairs = pair_items(mechanical, llm_timeline, overlap_threshold, title_threshold)
    machine_of_llm = {llm_index: machine_index for machine_index, llm_index in pairs.items()}

    timeline: list[dict[str, Any]] = []
    for index, llm_item in enumerate(llm_timeline):
        machine_index = machine_of_llm.get(index)
        if machine_index is None:
            item = dict(llm_item)
            item["flags"] = _dedupe([*(llm_item.get("flags") or []), LLM_UNMATCHED_FLAG])
            timeline.append(item)
            continue
        timeline.append(_restore_machine_fields(llm_item, mechanical[machine_index]))

    for index, machine_item in enumerate(mechanical):
        if index in pairs:
            continue
        restored = dict(machine_item)
        restored["flags"] = _dedupe([*(machine_item.get("flags") or []), LLM_DROPPED_FLAG])
        timeline.append(restored)

    timeline.sort(key=lambda item: (_as_float(item.get("start_sec")), _as_float(item.get("end_sec"))))
    return timeline, _build_diff(mechanical, llm_timeline, pairs)


def write_merge_diff(report_path: Path, diff: dict[str, Any]) -> Path:
    """`validation_report.json` に `merge_diff` を追記する（無ければ新規作成）。"""
    report: dict[str, Any] = {}
    if report_path.exists():
        try:
            loaded = read_json(report_path)
            if isinstance(loaded, dict):
                report = loaded
        except (OSError, ValueError):
            report = {}
    report["merge_diff"] = diff
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(report_path, report)
    return report_path


# --- 和集合 ----------------------------------------------------------------------


def run_name_for(path: Path) -> str:
    """`<session>/merge/timeline.json` -> `<session>` のディレクトリ名。"""
    resolved = Path(path).expanduser().resolve()
    if resolved.parent.name == MERGE_DIR_NAME:
        return resolved.parent.parent.name
    return resolved.parent.name or resolved.stem


def union_timelines(
    paths: list[str | Path],
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    title_similarity_threshold: float = DEFAULT_TITLE_SIMILARITY,
) -> dict[str, Any]:
    """複数実行の timeline を機械的に合成する。各項目に `runs` を残す。"""
    if len(paths) < 2:
        raise RuntimeError("--union には2つ以上の timeline.json を指定してください")

    items: list[dict[str, Any]] = []
    overviews: list[str] = []
    runs: list[str] = []
    for value in paths:
        path = Path(value).expanduser()
        if not path.exists():
            raise RuntimeError(f"timeline が見つかりません: {path}")
        doc = load_timeline(path)
        run = run_name_for(path)
        runs.append(run)
        overview = str(doc.get("overview") or "").strip()
        if overview:
            overviews.append(overview)
        for entry in doc.get("timeline") or []:
            if not isinstance(entry, dict):
                continue
            item = normalize_event(entry)
            item["runs"] = _dedupe([*(item.get("runs") or []), run])
            items.append(item)

    # 和集合では「別々の実行が同じ出来事を挙げたか」を見るので、出所は runs で判定する
    merged = merge_events(items, overlap_threshold, title_similarity_threshold, origin_key="runs")
    for item in merged:
        item.setdefault("runs", [])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overview": overviews[0] if overviews else "",
        "runs": runs,
        "source_files": [str(Path(value).expanduser()) for value in paths],
        "timeline": merged,
    }


# --- 音声の取り込み ---------------------------------------------------------------


def _audio_title(description: str, kind: str) -> str:
    text = " ".join(description.split())
    if len(text) > 40:
        text = text[:39] + "…"
    return text or f"音声（{kind}）"


def attach_audio(
    timeline: list[dict[str, Any]],
    audio: dict[str, Any],
) -> list[dict[str, Any]]:
    """音声解析の `segments[]` を `source: "audio"` の項目として timeline に足す。

    映像側の項目と時間が重ならないものには `audio_unconfirmed` を付ける。
    採用の判断はエージェントに残す（importance は `low` 固定）。
    """
    video_items = [item for item in timeline if item.get("source") != "audio"]
    added: list[dict[str, Any]] = []

    for segment in audio.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("kind") or "other")
        description = str(segment.get("description") or "")
        item = {
            "start_sec": _as_float(segment.get("start_sec")),
            "end_sec": _as_float(segment.get("end_sec")),
            "title": _audio_title(description, kind),
            "summary": description,
            "importance": "low",
            "confidence": segment.get("confidence") or "medium",
            "evidence": [],
            "sources": ["audio"],
            "flags": [],
            "source": "audio",
            "kind": kind,
        }
        overlapped = any(overlap_ratio(item, video) > 0 for video in video_items)
        if not overlapped:
            item["flags"] = ["audio_unconfirmed"]
        added.append(item)

    combined = [*timeline, *added]
    combined.sort(key=lambda item: (_as_float(item.get("start_sec")), _as_float(item.get("end_sec"))))
    return combined


# --- final.md --------------------------------------------------------------------


def _fmt_sec(value: Any) -> str:
    return f"{_as_float(value):.1f}"


def _evidence_text(item: dict[str, Any]) -> str:
    evidence = [str(value) for value in (item.get("evidence") or [])]
    sources = [str(value) for value in (item.get("sources") or [])]
    parts = []
    if evidence:
        parts.append(" / ".join(evidence))
    if sources:
        parts.append(f"（出所: {', '.join(sources)}）")
    return "".join(parts) if parts else "（根拠の記載なし）"


def render_final_md(doc: dict[str, Any]) -> str:
    """timeline から final.md（概要 / 見どころ候補 / 追加確認した範囲 / タイムライン要約 / 注意点）を作る。"""
    timeline = [item for item in (doc.get("timeline") or []) if isinstance(item, dict)]
    highlights = [item for item in timeline if item.get("importance") != "low"]

    lines: list[str] = ["## 概要", ""]
    lines.append(str(doc.get("overview") or "").strip() or "（概要は未記入）")
    lines.append("")

    lines.append("## 見どころ候補")
    lines.append("")
    if not highlights:
        lines.append("（importance が low 以外の項目はありません）")
        lines.append("")
    for index, item in enumerate(highlights, start=1):
        start, end = _fmt_sec(item.get("start_sec")), _fmt_sec(item.get("end_sec"))
        lines.append(f"### {index}. {item.get('title') or '(タイトルなし)'} ({start}s - {end}s)")
        lines.append(f"- **title**: {item.get('title') or ''}")
        lines.append(f"- **start_sec** / **end_sec**: {start} / {end}")
        lines.append(f"- **summary**: {item.get('summary') or ''}")
        lines.append(f"- **根拠**: {_evidence_text(item)}")
        details = [f"importance={item.get('importance')}", f"confidence={item.get('confidence')}"]
        if item.get("flags"):
            details.append("flags=" + ",".join(str(flag) for flag in item["flags"]))
        if item.get("runs"):
            details.append("runs=" + ",".join(str(run) for run in item["runs"]))
        lines.append(f"- **補足**: {' / '.join(details)}")
        lines.append("")

    lines.append("## 追加確認した範囲")
    lines.append("")
    spans: dict[str, list[float]] = {}
    for item in timeline:
        for source in item.get("sources") or []:
            key = str(source)
            span = spans.setdefault(key, [_as_float(item.get("start_sec")), _as_float(item.get("end_sec"))])
            span[0] = min(span[0], _as_float(item.get("start_sec")))
            span[1] = max(span[1], _as_float(item.get("end_sec")))
    if spans:
        for source in sorted(spans):
            low, high = spans[source]
            count = sum(1 for item in timeline if source in (item.get("sources") or []))
            lines.append(f"- {source}: {low:.1f}s-{high:.1f}s（{count}件）")
    else:
        lines.append("（記録なし）")
    lines.append("")

    lines.append("## タイムライン要約")
    lines.append("")
    if timeline:
        for item in timeline:
            marker = " [音声]" if item.get("source") == "audio" else ""
            lines.append(
                f"- {_fmt_sec(item.get('start_sec'))}s-{_fmt_sec(item.get('end_sec'))}s"
                f" [{item.get('importance')}]{marker} {item.get('title') or ''}"
            )
    else:
        lines.append("（項目なし）")
    lines.append("")

    lines.append("## 注意点")
    lines.append("")
    notes: list[str] = []
    for item in timeline:
        if item.get("flags"):
            notes.append(
                f"- {_fmt_sec(item.get('start_sec'))}s-{_fmt_sec(item.get('end_sec'))}s"
                f" {item.get('title') or ''}: {', '.join(str(flag) for flag in item['flags'])}"
            )
    for label in doc.get("hypothesis_rejected") or []:
        notes.append(f"- {label}: 全体把握での仮説が詳細解析で反証された（hypothesis_rejected）")
    if notes:
        lines.extend(notes)
    else:
        lines.append("（フラグの付いた項目はありません）")
    lines.append("")
    return "\n".join(lines)


# --- CLI 本体 --------------------------------------------------------------------


@dataclass
class MergeOptions:
    """統合の設定。CLI の argparse.Namespace と同じ属性名を持つ。"""

    session: str | None = None
    inputs: list[str] | None = None
    union: list[str] | None = None
    audio: str | None = None
    output: str | None = None
    output_md: str | None = None
    final_md: bool = False
    objective: str | None = None
    domain: str | None = None
    no_llm: bool = False
    llm_chunk: int = DEFAULT_LLM_CHUNK
    backend: str = DEFAULT_BACKEND
    model: str | None = None
    api_key: str | None = None
    strict_json: bool = False
    prompt: str | None = None
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD
    title_similarity: float = DEFAULT_TITLE_SIMILARITY
    report_output: str | None = None

    @classmethod
    def from_namespace(cls, namespace: Any) -> "MergeOptions":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in vars(namespace).items() if key in known})


def _resolve_session_dir(options: MergeOptions, fallback: Path | None) -> Path | None:
    if options.session:
        return Path(options.session).expanduser().resolve()
    if fallback is not None:
        session = Session.find(fallback)
        if session is not None:
            return session.root
    return None


def _default_output_path(session_dir: Path | None, fallback: Path, filename: str) -> Path:
    if session_dir is not None:
        return session_dir / MERGE_DIR_NAME / filename
    return fallback.parent / filename


def run_merge(options: MergeOptions) -> int:
    objective = resolve_text_arg(options.objective) or DEFAULT_OBJECTIVE
    domain = load_domain(options.domain) if options.domain else None

    if options.union:
        return _run_union(options)

    collected = collect_events(options.session, options.inputs)
    if not collected.files:
        raise RuntimeError("解析結果が1件も見つかりませんでした（*_validated.json / *_analysis.json）")
    log(f"[開始] 統合: {len(collected.files)}ファイル / {len(collected.events)}件")

    fallback = Path(collected.files[0])
    session_dir = _resolve_session_dir(options, fallback)
    output_path = (
        Path(options.output).expanduser().resolve()
        if options.output
        else _default_output_path(session_dir, fallback, TIMELINE_FILENAME)
    )

    mechanical = merge_events(
        collected.events, options.overlap_threshold, options.title_similarity
    )
    mechanical_path = output_path.parent / MECHANICAL_FILENAME
    mechanical_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(mechanical_path, build_timeline_doc(mechanical, collected=collected))
    log(f"  機械統合: {len(collected.events)}件 -> {len(mechanical)}件  {mechanical_path}")

    timeline = mechanical
    overview = ""
    if options.no_llm:
        log("  LLM統合: スキップ（--no-llm）")
    else:
        result = llm_merge(
            mechanical,
            objective=objective,
            domain=domain,
            backend=options.backend,
            model=options.model,
            api_key=options.api_key,
            strict_json=options.strict_json,
            chunk_size=options.llm_chunk,
            prompt_path=options.prompt,
            session_dir=session_dir,
            raw_output_path=output_path.with_suffix(".raw.txt"),
            records_dir=output_path.parent,
        )
        overview = result["overview"]
        # LLM の出力をそのまま採らず、機械統合の結果と照合してから採用する
        timeline, diff = reconcile_llm_timeline(
            mechanical, result["timeline"], options.overlap_threshold, options.title_similarity
        )
        report_path = (
            Path(options.report_output).expanduser().resolve()
            if options.report_output
            else output_path.parent / REPORT_FILENAME
        )
        write_merge_diff(report_path, diff)
        log(
            f"  LLM統合: {diff['n_before']}件 -> {diff['n_after']}件"
            f"（消えた {len(diff['dropped'])} / 増えた {len(diff['added'])} / 時間変化 {len(diff['moved'])}）"
        )
        if diff["dropped"]:
            log(f"  消えた {len(diff['dropped'])} 件は {LLM_DROPPED_FLAG} を付けて timeline に戻しました")
        log(f"  差分: {report_path}")

    if options.audio:
        audio_path = Path(options.audio).expanduser()
        audio = read_json(audio_path)
        before = len(timeline)
        timeline = attach_audio(timeline, audio if isinstance(audio, dict) else {})
        unconfirmed = sum(1 for item in timeline if "audio_unconfirmed" in (item.get("flags") or []))
        log(f"  音声: {len(timeline) - before}件を追加（audio_unconfirmed {unconfirmed}件）")

    doc = build_timeline_doc(timeline, overview=overview, collected=collected)
    write_json(output_path, doc)
    log(f"  timeline: {output_path}")

    if options.final_md or options.output_md:
        _write_final_md(options, doc, session_dir, output_path)

    log(f"[完了] {len(timeline)}件")
    return 0


def _run_union(options: MergeOptions) -> int:
    log(f"[開始] 和集合: {len(options.union)}実行")
    doc = union_timelines(options.union, options.overlap_threshold, options.title_similarity)

    fallback = Path(options.union[0]).expanduser()
    session_dir = _resolve_session_dir(options, fallback)
    output_path = (
        Path(options.output).expanduser().resolve()
        if options.output
        else _default_output_path(session_dir, fallback, UNION_FILENAME)
    )

    timeline = doc["timeline"]
    if options.audio:
        audio = read_json(Path(options.audio).expanduser())
        timeline = attach_audio(timeline, audio if isinstance(audio, dict) else {})
        doc["timeline"] = timeline

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, doc)
    log(f"  timeline_union: {output_path}  {len(timeline)}件")

    if options.final_md or options.output_md:
        _write_final_md(options, doc, session_dir, output_path)

    log(f"[完了] {len(timeline)}件")
    return 0


def _write_final_md(
    options: MergeOptions,
    doc: dict[str, Any],
    session_dir: Path | None,
    output_path: Path,
) -> Path:
    if options.output_md:
        md_path = Path(options.output_md).expanduser().resolve()
    elif session_dir is not None:
        md_path = session_dir / FINAL_MD_FILENAME
    else:
        md_path = output_path.parent / FINAL_MD_FILENAME
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_final_md(doc), encoding="utf-8")
    log(f"  final.md: {md_path}")
    return md_path
