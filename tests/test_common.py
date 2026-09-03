"""avs.common のテスト（APIキー解決）。"""

from __future__ import annotations

import pytest

from avs.common import BACKEND_ENV_NAMES, resolve_api_key


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
