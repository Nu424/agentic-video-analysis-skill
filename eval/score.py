#!/usr/bin/env python
"""正解データ（ground truth）に対して本スキルの出力を採点する。

正解データの形式は `eval/README.md` §10、`eval/ground_truth.example.json` を参照。

判定ルール（正解データ自身に埋め込まれたルールをそのまま使う。ここでは判断しない）:

- 検出: 出力エントリの `[start_sec, end_sec]` が `events[].match.window` と重なり、
  かつエントリのテキスト（title + summary + evidence/visual の連結）が
  `events[].match.any` のいずれかの正規表現にマッチする。
- 誤認: `negatives[].pattern`（任意で `window`）にテキストがマッチする。
  `verdict == "誤り"` のものだけを誤認数に数える。`verdict == "根拠なし"` は
  別集計として表示するが、誤認数には数えない（映像から判定できないだけで、
  誤りとは断定できないため）。

入力アダプタ（`load_entries`）は次の 3 形状を受け付ける。

- `timeline.json` / `timeline_union.json`: トップレベル `timeline[]`
  （`start_sec`, `end_sec`, `title`, `summary`, `evidence[]`, ...）
- `final.json` 形式: `result.timeline[]`（移植元 `try-gemini-agenticvideo` の形状）
- detail の `*_analysis.json` / `*_validated.json`: トップレベル `events[]`
  （`start_sec`, `end_sec`, `title`, `summary`, `visual[]`, ...）

使い方:

  python eval/score.py <session>/merge/timeline.json --gt eval/fixtures/<name>/ground_truth.json

  python eval/score.py run1/timeline.json run2/timeline.json run3/timeline.json \
      --gt eval/fixtures/<name>/ground_truth.json --verbose

  python eval/score.py <session>/merge/timeline.json --gt eval/fixtures/<name>/ground_truth.json \
      --session output/agentic_sessions/<name> --json

コスト集計は `--session` に指定したセッションディレクトリの `usage.jsonl`
（`avs/analysis.py` が書く 1 行 1 呼び出しの監査ログ）から出す。省略時はコストを出さない。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

# --- UTF-8 出力（Windows の既定コンソールは cp932 で日本語が落ちるため） -----------


def configure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


# --- エラー型 -----------------------------------------------------------------


class GroundTruthError(ValueError):
    """正解データ（ground_truth.json）の形式が不正なときに送出する。"""


class ScoreError(ValueError):
    """採点対象の出力ファイルの形式が不正なときに送出する。"""


# --- 正解データの読み込みと検証 --------------------------------------------------


def load_ground_truth(path: str | Path) -> dict[str, Any]:
    """正解データ JSON を読み込み、`eval/README.md` §10 の形式を検証する。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise GroundTruthError(f"正解データを読み込めません: {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise GroundTruthError(f"正解データが正しい JSON ではありません: {p}: {exc}") from exc

    if not isinstance(data, dict):
        raise GroundTruthError(f"正解データの形式が不正です（トップレベルがオブジェクトではありません）: {p}")

    events = data.get("events")
    if not isinstance(events, list):
        raise GroundTruthError(f"正解データの形式が不正です（events[] がありません）: {p}")
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise GroundTruthError(f"正解データの形式が不正です（events[{i}] がオブジェクトではありません）: {p}")
        for key in ("id", "start", "end", "cat", "name", "match"):
            if key not in ev:
                raise GroundTruthError(f"正解データの形式が不正です（events[{i}] に '{key}' がありません）: {p}")
        match = ev["match"]
        if not isinstance(match, dict) or "window" not in match or "any" not in match:
            raise GroundTruthError(
                f"正解データの形式が不正です（events[{i}].match に window/any がありません）: {p}"
            )
        window = match["window"]
        if not isinstance(window, list) or len(window) != 2:
            raise GroundTruthError(
                f"正解データの形式が不正です（events[{i}].match.window は [start, end] である必要があります）: {p}"
            )
        any_patterns = match["any"]
        if not isinstance(any_patterns, list) or not any_patterns:
            raise GroundTruthError(
                f"正解データの形式が不正です（events[{i}].match.any は空でないリストである必要があります）: {p}"
            )

    negatives = data.get("negatives", [])
    if not isinstance(negatives, list):
        raise GroundTruthError(f"正解データの形式が不正です（negatives は配列である必要があります）: {p}")
    for i, neg in enumerate(negatives):
        if not isinstance(neg, dict):
            raise GroundTruthError(f"正解データの形式が不正です（negatives[{i}] がオブジェクトではありません）: {p}")
        for key in ("name", "pattern", "verdict"):
            if key not in neg:
                raise GroundTruthError(f"正解データの形式が不正です（negatives[{i}] に '{key}' がありません）: {p}")
        if neg["verdict"] not in ("誤り", "根拠なし"):
            raise GroundTruthError(
                f"正解データの形式が不正です（negatives[{i}].verdict は '誤り' または '根拠なし' である必要があります）: {p}"
            )
        window = neg.get("window")
        if window is not None and (not isinstance(window, list) or len(window) != 2):
            raise GroundTruthError(
                f"正解データの形式が不正です（negatives[{i}].window は [start, end] である必要があります）: {p}"
            )

    data["negatives"] = negatives
    return data


# --- 出力ファイルの読み込み（入力アダプタ） --------------------------------------

# エントリのテキストに連結するキー。スカラーはそのまま、リストは要素を連結する。
# 本スキルの timeline / detail スキーマ（title, summary, evidence, visual）に加え、
# 移植元ツールの形状（name, visual_evidence）も汎用的に受け付ける。
_TEXT_SCALAR_KEYS = ("title", "name", "summary")
_TEXT_LIST_KEYS = ("evidence", "visual", "visual_evidence")


def _entry_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in _TEXT_SCALAR_KEYS:
        value = item.get(key)
        if value:
            parts.append(str(value))
    for key in _TEXT_LIST_KEYS:
        value = item.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
    return " ".join(parts)


def _extract_items(data: Any, path: Path) -> list[Any]:
    """出力 JSON のトップレベル形状を吸収し、エントリのリストを返す。"""
    if not isinstance(data, dict):
        raise ScoreError(f"{path}: JSON のトップレベルがオブジェクトではありません")

    result = data.get("result")
    if isinstance(result, dict) and isinstance(result.get("timeline"), list):
        return result["timeline"]  # final.json 形式（移植元ツール）

    timeline = data.get("timeline")
    if isinstance(timeline, list):
        return timeline  # timeline.json / timeline_union.json

    events = data.get("events")
    if isinstance(events, list):
        return events  # detail の *_analysis.json / *_validated.json

    raise ScoreError(
        f"{path}: 既知の出力形式（timeline[] / events[] / result.timeline[]）が見つかりません"
    )


def load_entries(path: str | Path) -> list[tuple[float, float, str]]:
    """出力 JSON から `[(start_sec, end_sec, text), ...]` を取り出す（入力アダプタ）。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScoreError(f"出力ファイルを読み込めません: {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ScoreError(f"出力ファイルが正しい JSON ではありません: {p}: {exc}") from exc

    items = _extract_items(data, p)
    entries: list[tuple[float, float, str]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start_sec"])
            end = float(item["end_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScoreError(f"{p}: エントリ[{i}] に start_sec/end_sec がありません") from exc
        entries.append((start, end, _entry_text(item)))
    return entries


def overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 <= b1 and b0 <= a1


# --- 採点 -----------------------------------------------------------------------


def score_entries(entries: list[tuple[float, float, str]], gt: dict[str, Any]) -> dict[str, Any]:
    """1 実行分のエントリ列を正解データと照合する。"""
    detected: list[str] = []
    missed: list[dict[str, str]] = []
    for ev in gt["events"]:
        w0, w1 = ev["match"]["window"]
        patterns = ev["match"]["any"]
        hit = any(
            overlaps(s, e, w0, w1) and any(re.search(pat, t) for pat in patterns)
            for s, e, t in entries
        )
        if hit:
            detected.append(ev["id"])
        else:
            missed.append({"id": ev["id"], "name": ev["name"]})

    false_positives: list[dict[str, Any]] = []  # verdict == 誤り
    no_basis: list[dict[str, Any]] = []  # verdict == 根拠なし
    for neg in gt["negatives"]:
        window = neg.get("window")
        for s, e, t in entries:
            if window and not overlaps(s, e, window[0], window[1]):
                continue
            if re.search(neg["pattern"], t):
                hit_info = {"name": neg["name"], "at": [s, e]}
                if neg["verdict"] == "誤り":
                    false_positives.append(hit_info)
                else:
                    no_basis.append(hit_info)
                break

    by_cat: dict[str, list[int]] = {}
    for ev in gt["events"]:
        cat = ev["cat"]
        bucket = by_cat.setdefault(cat, [0, 0])
        bucket[1] += 1
        if ev["id"] in detected:
            bucket[0] += 1

    n_events = len(gt["events"])
    return {
        "n_entries": len(entries),
        "n_events": n_events,
        "n_detected": len(detected),
        "recall_pct": round(100 * len(detected) / n_events, 1) if n_events else 0.0,
        "detected": detected,
        "missed": missed,
        "by_cat": {c: {"detected": v[0], "total": v[1]} for c, v in sorted(by_cat.items())},
        "false_positives": false_positives,
        "n_false_positives": len(false_positives),
        "no_basis": no_basis,
    }


def score_file(path: str | Path, gt: dict[str, Any]) -> dict[str, Any]:
    entries = load_entries(path)
    result = score_entries(entries, gt)
    result["file"] = str(path)
    return result


# --- コスト集計（usage.jsonl） ---------------------------------------------------

# usage.jsonl の "name"（例: "cand_a_analysis", "overview_full_analysis"）を
# 大まかなステップに分類するためのキーワード。avs/session.py の分類と合わせてある。
_STEP_KEYWORDS = ("overview", "chapters", "detail", "zoom", "refine", "audio", "merge")


def _classify_step(name: str) -> str:
    lowered = name.lower()
    for keyword in _STEP_KEYWORDS:
        if keyword in lowered:
            return keyword
    return "other"


def _step_of(record: dict[str, Any]) -> str:
    """usage.jsonl の 1 行のステップ名。`step` があればそれを使う（avs/session.py: step_of と同じ規則）。

    `step` は呼び出し元がプロンプト名から決めた正確な値（`cand_00_analysis` のような
    出力名からのキーワード推測では detail が other に落ちるため）。`eval/` は `avs` に
    依存しない方針のため import はせず、同じ規則をここに書く。
    """
    step = record.get("step")
    if isinstance(step, str) and step:
        return step
    return _classify_step(str(record.get("name") or ""))


def load_usage_records(session_dir: str | Path) -> list[dict[str, Any]]:
    """`<session>/usage.jsonl` を読む（`avs/analysis.py` が書く 1 行 1 呼び出しの監査ログ）。"""
    usage_path = Path(session_dir) / "usage.jsonl"
    if not usage_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in usage_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # 壊れた行はスキップ（集計を止めない）
    return records


def summarize_cost(records: list[dict[str, Any]]) -> dict[str, Any]:
    """呼び出し回数・トークン内訳・USD（実測/概算を分ける）・latency をステップ別に集計する。"""
    totals = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    actual_cost = 0.0
    estimated_cost = 0.0
    unknown_cost_calls = 0
    latency_sec = 0.0
    by_step: dict[str, dict[str, Any]] = {}

    for record in records:
        usage = record.get("usage") or {}
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
        latency_sec += float(record.get("latency_sec") or 0.0)

        cost = record.get("cost_usd")
        is_estimate = bool(record.get("cost_is_estimate"))
        if cost is None:
            unknown_cost_calls += 1
        elif is_estimate:
            estimated_cost += float(cost)
        else:
            actual_cost += float(cost)

        step = _step_of(record)
        bucket = by_step.setdefault(step, {"calls": 0, "total_tokens": 0, "cost_usd": 0.0})
        bucket["calls"] += 1
        bucket["total_tokens"] += int(usage.get("total_tokens") or 0)
        if cost is not None:
            bucket["cost_usd"] += float(cost)

    for bucket in by_step.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)

    return {
        "calls": len(records),
        "tokens": totals,
        "cost_usd_actual": round(actual_cost, 6),
        "cost_usd_estimated": round(estimated_cost, 6),
        "cost_usd_total": round(actual_cost + estimated_cost, 6),
        "cost_has_estimate": estimated_cost > 0 or unknown_cost_calls > 0,
        "unknown_cost_calls": unknown_cost_calls,
        "latency_sec": round(latency_sec, 3),
        "by_step": by_step,
    }


# --- 統計（標本標準偏差） -------------------------------------------------------


def sample_stdev(values: list[float]) -> float:
    """標本標準偏差（n-1）。`statistics.pstdev`（母標準偏差）ではなくこちらを使う。"""
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


# --- CLI ------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="正解データ（ground truth）に対して本スキルの出力を採点する。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  python eval/score.py <session>/merge/timeline.json"
            " --gt eval/fixtures/<name>/ground_truth.json\n"
            "  python eval/score.py run1/timeline.json run2/timeline.json"
            " --gt eval/fixtures/<name>/ground_truth.json --verbose\n"
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help=(
            "採点対象の出力ファイル（timeline.json / timeline_union.json / final.json /"
            " *_analysis.json / *_validated.json）。複数指定すると各行 + 平均・標準偏差を出す"
        ),
    )
    parser.add_argument("--gt", required=True, help="正解データ（ground_truth.json）へのパス")
    parser.add_argument(
        "--session",
        help="usage.jsonl を含むセッションディレクトリ。指定するとコスト集計を出す（省略時はコストを出さない）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="見逃し・誤認・根拠なしの詳細とコスト内訳を表示する",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="結果を JSON で標準出力する（--verbose の指定は無視される）",
    )
    return parser


def _print_table(rows: list[dict[str, Any]], cat_ids: list[str]) -> None:
    headers = ["file", "entries", "recall", "%"] + list(cat_ids) + ["誤り", "根拠なし"]
    widths = [max(len(h), 10) for h in headers]
    widths[0] = max(widths[0], max((len(r["file"]) for r in rows), default=10))

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(w) for cell, w in zip(cells, widths))

    print(fmt_row(headers))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for r in rows:
        cells = [
            r["file"],
            str(r["n_entries"]),
            f"{r['n_detected']}/{r['n_events']}",
            f"{r['recall_pct']}",
        ]
        for c in cat_ids:
            v = r["by_cat"].get(c)
            cells.append(f"{v['detected']}/{v['total']}" if v else "-")
        cells.append(str(r["n_false_positives"]))
        cells.append(str(len(r["no_basis"])))
        print(fmt_row(cells))


def _print_verbose(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        print(f"\n## {r['file']}")
        missed = r["missed"]
        print("  見逃し:", ", ".join(f"{m['id']}({m['name']})" for m in missed) or "なし")
        fps = r["false_positives"]
        print(
            "  誤認:",
            ", ".join(f"{h['name']}@{h['at'][0]}-{h['at'][1]}" for h in fps) or "なし",
        )
        nb = r["no_basis"]
        print(
            "  根拠なし:",
            ", ".join(f"{h['name']}@{h['at'][0]}-{h['at'][1]}" for h in nb) or "なし",
        )
        cost = r.get("cost")
        if cost:
            t = cost["tokens"]
            print(
                f"  コスト: calls={cost['calls']} "
                f"input={t['input_tokens']} output={t['output_tokens']} "
                f"reasoning={t['reasoning_tokens']} total={t['total_tokens']} "
                f"usd_actual={cost['cost_usd_actual']} usd_estimated={cost['cost_usd_estimated']} "
                f"usd_total={cost['cost_usd_total']}"
                + ("（概算を含む）" if cost["cost_has_estimate"] else "")
            )
            for step, bucket in sorted(cost["by_step"].items()):
                print(
                    f"    {step}: calls={bucket['calls']} "
                    f"total_tokens={bucket['total_tokens']} cost_usd={bucket['cost_usd']}"
                )


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdout()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        gt = load_ground_truth(args.gt)
        rows = [score_file(p, gt) for p in args.paths]
    except (GroundTruthError, ScoreError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    cost_summary: dict[str, Any] | None = None
    if args.session:
        records = load_usage_records(args.session)
        cost_summary = summarize_cost(records)
        for r in rows:
            r["cost"] = cost_summary
    else:
        for r in rows:
            r["cost"] = None

    cat_ids = sorted({c for r in rows for c in r["by_cat"]})

    recall_values = [r["recall_pct"] for r in rows]
    fp_values = [float(r["n_false_positives"]) for r in rows]
    summary = {
        "n_runs": len(rows),
        "recall_pct_mean": round(statistics.mean(recall_values), 1) if recall_values else 0.0,
        "recall_pct_sd": round(sample_stdev(recall_values), 2),
        "false_positives_mean": round(statistics.mean(fp_values), 2) if fp_values else 0.0,
        "false_positives_sd": round(sample_stdev(fp_values), 2),
    }

    if args.json:
        print(json.dumps({"runs": rows, "summary": summary, "cost": cost_summary}, ensure_ascii=False, indent=2))
        return 0

    _print_table(rows, cat_ids)
    if len(rows) > 1:
        print(
            f"\n平均: recall {summary['recall_pct_mean']}% (sd {summary['recall_pct_sd']})"
            f"  誤認 {summary['false_positives_mean']} (sd {summary['false_positives_sd']})"
        )
    if cost_summary:
        t = cost_summary["tokens"]
        print(
            f"\nコスト（session={args.session}）: calls={cost_summary['calls']} "
            f"tokens(input/output/reasoning/total)="
            f"{t['input_tokens']}/{t['output_tokens']}/{t['reasoning_tokens']}/{t['total_tokens']} "
            f"usd_actual={cost_summary['cost_usd_actual']} "
            f"usd_estimated={cost_summary['cost_usd_estimated']} "
            f"usd_total={cost_summary['cost_usd_total']}"
            + ("（概算を含む）" if cost_summary["cost_has_estimate"] else "")
        )
    if args.verbose:
        _print_verbose(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
