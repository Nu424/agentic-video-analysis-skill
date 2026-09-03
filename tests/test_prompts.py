"""avs.prompts のスキーマ・ドメイン定義・プロンプトファイルのテスト。

実 API は呼ばない。プロンプトファイルは実ファイルを読んで検査する。
（プロンプト組み立ての順序そのものは tests/test_analysis.py にもテストがある）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avs.prompts import (
    SCHEMA_PLACEHOLDER,
    SCHEMAS,
    DomainError,
    OBJECTIVE_PLACEHOLDER,
    api_schema,
    apply_objective,
    apply_schema,
    assemble_prompt,
    build_domain_block,
    build_importance_block,
    load_domain,
    render_schema_example,
    schema_for_prompt,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "agentic-video-analysis-skill"
PROMPTS_DIR = SKILL_DIR / "prompts"
DOMAIN_EXAMPLE = SKILL_DIR / "examples" / "domain.example.json"

PROMPT_NAMES = ["overview", "detail", "refine", "zoom", "merge", "audio", "chapters"]


# --- スキーマ例の生成 ----------------------------------------------------------


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_render_schema_example_is_valid_json(name: str):
    rendered = render_schema_example(SCHEMAS[name])
    parsed = json.loads(rendered)  # そのまま JSON として読めること
    assert isinstance(parsed, dict)
    # required に挙げた主要キーは例にも必ず現れる
    for key in SCHEMAS[name].get("required", []):
        assert key in parsed


def test_render_schema_example_fills_nested_objects():
    parsed = json.loads(render_schema_example(SCHEMAS["detail"]))
    event = parsed["events"][0]
    assert set(["start_sec", "end_sec", "title", "summary", "confidence"]) <= set(event)
    assert isinstance(event["start_sec"], (int, float))
    assert event["confidence"] in ("high", "medium", "low")
    assert isinstance(event["visual"], list)
    assert isinstance(event["start_bounds"], list) and len(event["start_bounds"]) == 2
    assert parsed["hypothesis_verdict"] in ("confirmed", "partially", "rejected", "n/a")


def test_render_schema_example_keeps_empty_array_examples():
    # remaining_questions は空配列を例にしている（現行プロンプトと同じ見た目）
    parsed = json.loads(render_schema_example(SCHEMAS["refine"]))
    assert parsed["remaining_questions"] == []


def test_render_schema_example_keeps_short_arrays_on_one_line():
    # 現行プロンプトと同じ見た目（[38.8, 39.0] のように短い配列は1行）
    rendered = render_schema_example(SCHEMAS["detail"])
    assert '"start_bounds": [38.8, 39.0]' in rendered
    assert '"zoom_targets": [39.4]' in rendered


def test_api_schema_strips_examples_but_keeps_structure():
    stripped = api_schema(SCHEMAS["overview"])
    text = json.dumps(stripped, ensure_ascii=False)
    assert '"examples"' not in text
    assert stripped["properties"]["candidates"]["items"]["properties"]["priority"]["enum"] == [
        "high",
        "medium",
        "low",
    ]
    # 元のスキーマは壊さない（コピーを返す）
    assert "examples" in SCHEMAS["overview"]["properties"]["summary"]
    assert api_schema(None) is None


# --- stem からのスキーマ引き当て ------------------------------------------------


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_schema_for_prompt_known_stems(name: str):
    assert schema_for_prompt(PROMPTS_DIR / f"{name}.txt") is SCHEMAS[name]


def test_schema_for_prompt_unknown_stem_returns_none():
    # ユーザー自作プロンプトを許すため、未知の stem はエラーにせず None
    assert schema_for_prompt("prompts/my_custom_prompt.txt") is None


def test_apply_schema_without_placeholder_or_schema_is_noop():
    assert apply_schema("プレースホルダなし", SCHEMAS["zoom"]) == "プレースホルダなし"
    body = f"```json\n{SCHEMA_PLACEHOLDER}\n```"
    assert apply_schema(body, None) == body


# --- プロンプトファイル本体 ------------------------------------------------------


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_prompt_file_exists_and_has_placeholders(name: str):
    body = (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    assert body.startswith(f"解析の目的: {OBJECTIVE_PLACEHOLDER}")
    assert SCHEMA_PLACEHOLDER in body
    # タイル読解ルールはスクリプトが付けるので、プロンプト本文には書かない
    assert "## タイル画像の読み方" not in body


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_prompt_file_has_no_placeholder_left_after_substitution(name: str):
    path = PROMPTS_DIR / f"{name}.txt"
    body = path.read_text(encoding="utf-8")
    rendered = apply_schema(apply_objective(body, "テスト目的"), schema_for_prompt(path))
    assert "{{" not in rendered
    assert "テスト目的" in rendered
    # 埋め込まれた JSON 例がコードフェンスの中にあること
    assert "```json\n{\n" in rendered


# --- ドメイン定義 ---------------------------------------------------------------


VALID_DOMAIN = {
    "name": "テストドメイン",
    "description": "説明文",
    "hud_notes": "画面左下に数値表示",
    "watchlist": ["画面左下の数値表示の増減"],
    "vocabulary": {"用語A": "画面が白く光る"},
    "negatives": [
        {"name": "映像に無い要素の登場", "pattern": "要素[AB]", "reason": "書かれやすい"},
        {"name": "冒頭の捏造", "pattern": "開始", "window": [0, 5]},
    ],
    "importance_rubric": "high: … medium: … low: …",
}


def _write_domain(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "domain.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_domain_reads_valid_file(tmp_path):
    domain = load_domain(_write_domain(tmp_path, VALID_DOMAIN))
    assert domain["name"] == "テストドメイン"
    assert len(domain["negatives"]) == 2


def test_load_domain_example_file_is_valid():
    domain = load_domain(DOMAIN_EXAMPLE)
    assert domain["watchlist"]
    assert domain["negatives"]


def test_load_domain_missing_file(tmp_path):
    with pytest.raises(DomainError) as excinfo:
        load_domain(tmp_path / "nope.json")
    assert "見つかりません" in str(excinfo.value)


def test_load_domain_broken_json(tmp_path):
    path = tmp_path / "domain.json"
    path.write_text("{ これはJSONではない", encoding="utf-8")
    with pytest.raises(DomainError) as excinfo:
        load_domain(path)
    assert "JSON" in str(excinfo.value)


def test_load_domain_rejects_invalid_regex(tmp_path):
    payload = {"negatives": [{"name": "壊れた正規表現", "pattern": "(未閉じ"}]}
    with pytest.raises(DomainError) as excinfo:
        load_domain(_write_domain(tmp_path, payload))
    message = str(excinfo.value)
    assert "正規表現" in message  # 日本語のエラー文
    assert "negatives[0].pattern" in message


def test_load_domain_rejects_bad_shapes(tmp_path):
    cases = [
        ([], "オブジェクト"),
        ({"watchlist": "文字列"}, "watchlist"),
        ({"vocabulary": ["配列"]}, "vocabulary"),
        ({"negatives": [{"pattern": "x"}]}, "name"),
        ({"negatives": [{"name": "n", "window": [5, 1]}]}, "window"),
        ({"hud_notes": 3}, "hud_notes"),
    ]
    for payload, expected in cases:
        with pytest.raises(DomainError) as excinfo:
            load_domain(_write_domain(tmp_path, payload))
        assert expected in str(excinfo.value)


def test_build_domain_block_sections():
    block = build_domain_block(VALID_DOMAIN)
    assert "## ドメインの手引き（テストドメイン）" in block
    assert "画面左下に数値表示" in block
    assert "- 画面左下の数値表示の増減" in block
    assert "- 用語A: 画面が白く光る" in block
    assert "## 誤認されやすい事象（映像で明確に確認できた場合のみ書く）" in block
    assert "- 映像に無い要素の登場" in block
    # pattern / window はバリデータが使うものなのでプロンプトには出さない
    assert "要素[AB]" not in block
    assert "importance_rubric" not in block
    assert "high: …" not in block


def test_build_domain_block_partial_and_empty():
    only_negatives = build_domain_block({"negatives": [{"name": "誤認事象"}]})
    assert "## ドメインの手引き" not in only_negatives
    assert "## 誤認されやすい事象" in only_negatives

    only_guide = build_domain_block({"watchlist": ["変化A"]})
    assert "## ドメインの手引き" in only_guide
    assert "## 誤認されやすい事象" not in only_guide

    assert build_domain_block(None) == ""
    assert build_domain_block({}) == ""
    assert build_domain_block({"negatives": []}) == ""


def test_build_importance_block():
    assert build_importance_block(VALID_DOMAIN).startswith("## 重要度の基準")
    assert "high: …" in build_importance_block(VALID_DOMAIN)
    assert build_importance_block({}) == ""
    assert build_importance_block(None) == ""


# --- assemble_prompt との結合 ---------------------------------------------------


def test_assemble_prompt_places_domain_after_hypothesis_before_context():
    prompt = assemble_prompt(
        "本文",
        "テスト目的",
        "TILE_CONTEXT",
        "追加文脈",
        "仮説メモ",
        retry_hint=True,
        domain=VALID_DOMAIN,
    )
    positions = [
        prompt.index("本文"),
        prompt.index("TILE_CONTEXT"),
        prompt.index("## 事前の仮説"),
        prompt.index("## ドメインの手引き"),
        prompt.index("## 誤認されやすい事象"),
        prompt.index("## 追加コンテキスト"),
        prompt.index("前回の出力はJSONとして不正だった"),
    ]
    assert positions == sorted(positions)


def test_assemble_prompt_without_domain_is_unchanged():
    # domain / schema を渡さないときの出力は、これらが無かった頃と完全に同一
    assert assemble_prompt("本文", "テスト目的", "CTX", None, None) == "本文\n\nCTX"
    with_none = assemble_prompt("本文", "テスト目的", "CTX", "文脈", "仮説", domain=None, schema=None)
    assert with_none == assemble_prompt("本文", "テスト目的", "CTX", "文脈", "仮説")


def test_assemble_prompt_applies_schema_to_body():
    body = f"本文 目的={OBJECTIVE_PLACEHOLDER}\n```json\n{SCHEMA_PLACEHOLDER}\n```"
    prompt = assemble_prompt("", "テスト目的", "CTX", None, None, schema=SCHEMAS["audio"]) or ""
    assert prompt  # 空本文でも落ちない
    prompt = assemble_prompt(body, "テスト目的", "CTX", None, None, schema=SCHEMAS["audio"])
    assert "{{" not in prompt
    assert '"segments"' in prompt


# --- 固有名詞ガード -------------------------------------------------------------
#
# このスキルは特定ドメインの知識を持たない（WORKPLAN-v2 §2-1）。
# ドメイン知識は domain.json で外から注入する設計なので、プロンプトと例には
# 特定のゲームタイトル・キャラクター・アイテム・競技の固有名詞が入ってはいけない。
# 下のリストは網羅ではなく「うっかり混入しやすい語」の見張り。

FORBIDDEN_TERMS = [
    # 特定タイトル・キャラクター
    "マリオ", "ルイージ", "ヨッシー", "ピーチ", "クッパ", "ポケモン", "ゼルダ",
    "スプラ", "モンハン", "フォートナイト", "マインクラフト", "Minecraft",
    "Mario", "Kart", "Pokemon", "Zelda", "Fortnite",
    # 特定ジャンル・競技の語彙
    "レース", "サーキット", "コース", "ラップ", "ドリフト", "順位", "コイン",
    "アイテム枠", "ゴール", "スタートダッシュ", "サッカー", "野球", "バスケ",
    "ゴールキーパー", "ホームラン", "スポーツ",
    # ジャンルの決め打ち
    "ゲーム実況", "ゲーム画面", "プレイヤーキャラ",
]


def _guarded_files() -> list[Path]:
    return sorted(PROMPTS_DIR.glob("*.txt")) + [DOMAIN_EXAMPLE]


def test_no_domain_specific_proper_nouns():
    hits: list[str] = []
    for path in _guarded_files():
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for term in FORBIDDEN_TERMS:
            if term.lower() in lowered:
                hits.append(f"{path.name}: {term}")
    assert not hits, f"固有名詞・ドメイン固有語が混入しています: {hits}"


def test_no_domain_specific_proper_nouns_in_schema_examples():
    for name in PROMPT_NAMES:
        rendered = render_schema_example(SCHEMAS[name]).lower()
        for term in FORBIDDEN_TERMS:
            assert term.lower() not in rendered, f"{name} スキーマの例に {term} が入っています"
