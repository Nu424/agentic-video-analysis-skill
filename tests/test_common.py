"""avs.common のテスト（APIキー解決 / slug / BOM 付き UTF-8 / 原子的な JSON 書き込み）。"""

from __future__ import annotations

import json

import pytest

from avs.common import (
    BACKEND_ENV_NAMES,
    read_json,
    read_text_utf8,
    resolve_api_key,
    safe_slug,
    write_json_atomic,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """環境変数と探索先（cwd / HOME）をテスト用に隔離する。"""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows の Path.home()
    return cwd, home


def test_explicit_key_wins(clean_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    assert resolve_api_key("OPENROUTER_API_KEY", explicit="from-cli") == "from-cli"


def test_env_var_wins_over_files(clean_env, monkeypatch):
    cwd, home = clean_env
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    (cwd / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\n", encoding="utf-8")
    (home / ".env.global").write_text("OPENROUTER_API_KEY=from-global\n", encoding="utf-8")
    assert resolve_api_key("OPENROUTER_API_KEY") == "from-env"


def test_dot_env_wins_over_env_global(clean_env):
    cwd, home = clean_env
    (cwd / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\n", encoding="utf-8")
    (home / ".env.global").write_text("OPENROUTER_API_KEY=from-global\n", encoding="utf-8")
    assert resolve_api_key("OPENROUTER_API_KEY") == "from-dotenv"


def test_env_global_is_last_resort(clean_env):
    _cwd, home = clean_env
    (home / ".env.global").write_text("GEMINI_API_KEY=from-global\n", encoding="utf-8")
    assert resolve_api_key("GEMINI_API_KEY") == "from-global"


def test_strips_export_prefix_and_quotes(clean_env):
    _cwd, home = clean_env
    (home / ".env.global").write_text(
        "# コメント\n"
        'export OPENROUTER_API_KEY="sk-or-v1-quoted"\n'
        "GEMINI_API_KEY='sk-single'\n",
        encoding="utf-8",
    )
    assert resolve_api_key("OPENROUTER_API_KEY") == "sk-or-v1-quoted"
    assert resolve_api_key("GEMINI_API_KEY") == "sk-single"


def test_ignores_other_keys_and_blank_values(clean_env):
    _cwd, home = clean_env
    (home / ".env.global").write_text(
        "OPENROUTER_API_KEY_OLD=nope\nOPENROUTER_API_KEY=\nGEMINI_API_KEY=yes\n",
        encoding="utf-8",
    )
    assert resolve_api_key("GEMINI_API_KEY") == "yes"
    assert resolve_api_key("OPENROUTER_API_KEY", required=False) is None


def test_missing_key_message_names_backend_and_key(clean_env):
    with pytest.raises(RuntimeError) as error:
        resolve_api_key("GEMINI_API_KEY", backend="gemini")
    message = str(error.value)
    assert "--backend gemini" in message
    assert "GEMINI_API_KEY" in message
    assert ".env.global" in message


def test_not_required_returns_none(clean_env):
    assert resolve_api_key("OPENROUTER_API_KEY", required=False) is None


def test_backend_env_names_mapping():
    assert BACKEND_ENV_NAMES == {
        "openrouter": "OPENROUTER_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }


# --- safe_slug -------------------------------------------------------------


def test_safe_slug_keeps_alnum_and_japanese_text():
    assert safe_slug("cand_a", "fallback") == "cand_a"
    assert safe_slug("ボス戦_1", "fallback") == "ボス戦_1"


def test_safe_slug_replaces_unsafe_chars_and_control_chars():
    assert safe_slug('a/b\\c:d*e?f"g<h>i|j', "fallback") == "a_b_c_d_e_f_g_h_i_j"
    assert safe_slug("a\x00b\x1fc", "fallback") == "a_b_c"


def test_safe_slug_strips_leading_trailing_whitespace_and_dots():
    assert safe_slug("  .cand_a.  ", "fallback") == "cand_a"
    assert safe_slug("...", "fallback") == "fallback"


def test_safe_slug_windows_reserved_names_get_trailing_underscore():
    for name in ("CON", "con", "Prn", "AUX", "NUL", "COM1", "com9", "LPT1", "lpt9"):
        assert safe_slug(name, "fallback") == f"{name}_"
    # 予約名でない語は変えない
    assert safe_slug("COM10", "fallback") == "COM10"
    assert safe_slug("COMPANY", "fallback") == "COMPANY"


def test_safe_slug_empty_or_none_uses_fallback():
    assert safe_slug("", "fallback") == "fallback"
    assert safe_slug(None, "fallback") == "fallback"
    assert safe_slug("   ", "fallback") == "fallback"


def test_safe_slug_truncates_to_80_chars():
    long_label = "a" * 200
    slug = safe_slug(long_label, "fallback")
    assert len(slug) == 80
    assert slug == "a" * 80


# --- BOM 付き UTF-8（Windows のエディタが付ける） ----------------------------------


def test_read_json_accepts_utf8_bom(tmp_path):
    path = tmp_path / "domain.json"
    path.write_text(json.dumps({"name": "テスト"}, ensure_ascii=False), encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM が付いている
    assert read_json(path) == {"name": "テスト"}


def test_read_json_still_accepts_plain_utf8(tmp_path):
    path = tmp_path / "plain.json"
    path.write_text('{"name": "テスト"}', encoding="utf-8")
    assert read_json(path) == {"name": "テスト"}


def test_read_text_utf8_strips_bom(tmp_path):
    path = tmp_path / "prompt.txt"
    path.write_text("# 見出し\n本文", encoding="utf-8-sig")
    text = read_text_utf8(path)
    assert text.startswith("# 見出し")
    assert "\ufeff" not in text


# --- 原子的な JSON 書き込み ---------------------------------------------------------


def test_write_json_atomic_writes_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "nested" / "summary.json"
    write_json_atomic(path, {"results": [1, 2, 3]})
    assert read_json(path) == {"results": [1, 2, 3]}
    assert [item.name for item in path.parent.iterdir()] == ["summary.json"]


def test_write_json_atomic_replaces_existing_file(tmp_path):
    path = tmp_path / "summary.json"
    write_json_atomic(path, {"n": 1})
    write_json_atomic(path, {"n": 2})
    assert read_json(path) == {"n": 2}
