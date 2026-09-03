"""avs.backends のテスト。

- openrouter: `urllib.request.urlopen` をモックしてリクエスト JSON を検証する
- gemini    : `sys.modules["google.genai"]` にダミーを注入して create() の引数を検証する

実 API は呼ばない。
"""

from __future__ import annotations

import base64
import io
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

from avs.backends import get_backend
from avs.backends.base import LLMRequest, MediaImage, MediaVideoClip, describe_media
from avs.backends.gemini import GeminiBackend, build_processing, format_offset
from avs.backends.openrouter import OpenRouterBackend
from avs.cost import estimate_cost_usd


# --- ヘルパ --------------------------------------------------------------------


def make_image(tmp_path: Path, name: str = "tile_000.jpg") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8\xff\xdbFAKEJPEG")
    return path


class FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def openrouter_response(text: str = '```json\n{"events": []}\n```', usage: dict | None = None) -> dict:
    return {
        "model": "google/gemini-3.7-flash",
        "choices": [{"message": {"content": text}}],
        "usage": usage
        if usage is not None
        else {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 100},
            "total_tokens": 1500,
            "cost": 0.0123,
        },
    }


def install_urlopen(monkeypatch, responses: list, recorded: list) -> None:
    """responses の各要素は dict（成功）か Exception（失敗）。"""
    import urllib.request

    queue = list(responses)

    def fake_urlopen(request, timeout=None):
        recorded.append(
            {
                "url": request.full_url,
                "headers": dict(request.headers),
                "timeout": timeout,
                "payload": json.loads(request.data.decode("utf-8")),
            }
        )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeHTTPResponse(json.dumps(item).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("avs.backends.openrouter.time.sleep", lambda _seconds: None)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://openrouter.ai", code, "boom", {}, io.BytesIO(b'{"error":"boom"}')
    )


# --- openrouter: リクエスト組み立て ---------------------------------------------


def test_openrouter_request_structure(tmp_path, monkeypatch):
    image = make_image(tmp_path)
    recorded: list = []
    install_urlopen(monkeypatch, [openrouter_response()], recorded)

    backend = OpenRouterBackend(api_key="k-test")
    backend.complete(LLMRequest(prompt="PROMPT本文", media=[MediaImage(path=image)]))

    call = recorded[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["timeout"] == 300
    assert call["headers"]["Authorization"] == "Bearer k-test"
    assert "Http-referer" in call["headers"] or "HTTP-Referer" in call["headers"]

    payload = call["payload"]
    assert payload["model"] == "google/gemini-3.7-flash"
    assert payload["usage"] == {"include": True}
    assert "response_format" not in payload  # strict_json でなければ送らない

    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "PROMPT本文"}
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == image.read_bytes()


def test_openrouter_mime_from_extension(tmp_path, monkeypatch):
    image = make_image(tmp_path, "tile.png")
    recorded: list = []
    install_urlopen(monkeypatch, [openrouter_response()], recorded)

    OpenRouterBackend(api_key="k").complete(
        LLMRequest(prompt="p", media=[MediaImage(path=image)])
    )
    url = recorded[0]["payload"]["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_openrouter_strict_json_sends_response_format(tmp_path, monkeypatch):
    recorded: list = []
    install_urlopen(monkeypatch, [openrouter_response()], recorded)
    schema = {"type": "object", "properties": {"events": {"type": "array"}}}

    OpenRouterBackend(api_key="k", strict_json=True).complete(
        LLMRequest(prompt="p", media=[], json_schema=schema)
    )

    response_format = recorded[0]["payload"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "analysis"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == schema


def test_openrouter_schema_without_strict_json_is_ignored(monkeypatch):
    recorded: list = []
    install_urlopen(monkeypatch, [openrouter_response()], recorded)

    OpenRouterBackend(api_key="k").complete(
        LLMRequest(prompt="p", media=[], json_schema={"type": "object"})
    )
    assert "response_format" not in recorded[0]["payload"]


# --- openrouter: レスポンスと usage/cost ---------------------------------------


def test_openrouter_normalizes_usage_and_cost(monkeypatch):
    recorded: list = []
    install_urlopen(monkeypatch, [openrouter_response(text="本文")], recorded)

    response = OpenRouterBackend(api_key="k").complete(LLMRequest(prompt="p", media=[]))

    assert response.text == "本文"
    assert response.backend == "openrouter"
    assert response.model == "google/gemini-3.7-flash"
    assert response.retries == 0
    assert response.usage["input_tokens"] == 1200
    assert response.usage["output_tokens"] == 300
    assert response.usage["reasoning_tokens"] == 100
    assert response.usage["total_tokens"] == 1500
    assert response.usage["cost_usd"] == 0.0123
    assert response.usage["raw"]["prompt_tokens"] == 1200

    # 実コストがあるので概算ではない
    assert estimate_cost_usd(response.model, response.usage) == (0.0123, False)


def test_openrouter_cost_falls_back_to_price_table(monkeypatch):
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "total_tokens": 2_000_000}
    recorded: list = []
    install_urlopen(monkeypatch, [openrouter_response(usage=usage)], recorded)

    response = OpenRouterBackend(api_key="k").complete(LLMRequest(prompt="p", media=[]))
    assert response.usage["cost_usd"] is None
    usd, is_estimate = estimate_cost_usd(response.model, response.usage)
    assert is_estimate is True
    assert usd == pytest.approx(0.75 + 3.75)


# --- openrouter: リトライ -------------------------------------------------------


def test_openrouter_retries_on_429_then_succeeds(monkeypatch):
    recorded: list = []
    install_urlopen(monkeypatch, [http_error(429), openrouter_response(text="ok")], recorded)

    response = OpenRouterBackend(api_key="k").complete(LLMRequest(prompt="p", media=[]))
    assert response.text == "ok"
    assert response.retries == 1
    assert len(recorded) == 2


def test_openrouter_retries_on_url_error_then_succeeds(monkeypatch):
    recorded: list = []
    install_urlopen(
        monkeypatch,
        [urllib.error.URLError("timed out"), http_error(503), openrouter_response(text="ok")],
        recorded,
    )

    response = OpenRouterBackend(api_key="k").complete(LLMRequest(prompt="p", media=[]))
    assert response.text == "ok"
    assert response.retries == 2


def test_openrouter_raises_after_three_failures(monkeypatch):
    recorded: list = []
    install_urlopen(monkeypatch, [http_error(500)] * 3, recorded)

    with pytest.raises(RuntimeError, match="3 回失敗"):
        OpenRouterBackend(api_key="k").complete(LLMRequest(prompt="p", media=[]))
    assert len(recorded) == 3


def test_openrouter_does_not_retry_on_400(monkeypatch):
    recorded: list = []
    install_urlopen(monkeypatch, [http_error(400)], recorded)

    with pytest.raises(RuntimeError, match="HTTP 400"):
        OpenRouterBackend(api_key="k").complete(LLMRequest(prompt="p", media=[]))
    assert len(recorded) == 1


def test_openrouter_rejects_video_clip(tmp_path):
    backend = OpenRouterBackend(api_key="k")
    clip = MediaVideoClip(video=tmp_path / "v.mp4", start_sec=1.0, end_sec=2.0, fps=5)
    with pytest.raises(RuntimeError, match="--backend gemini"):
        backend.complete(LLMRequest(prompt="p", media=[clip]))


def test_openrouter_supports_flags():
    backend = OpenRouterBackend(api_key="k")
    assert backend.supports_video_clip is False
    assert backend.supports_audio is False


# --- gemini: ダミー SDK ---------------------------------------------------------


class FakeState:
    def __init__(self, name):
        self.name = name


class FakeFile:
    def __init__(self, name="files/abc", state="ACTIVE"):
        self.name = name
        self.uri = f"https://generativelanguage.googleapis.com/v1beta/{name}"
        self.mime_type = "video/mp4"
        self.state = FakeState(state)


class FakeUsage:
    def __init__(self):
        self._values = {
            "total_input_tokens": 5000,
            "total_output_tokens": 400,
            "total_thought_tokens": 200,
            "total_tokens": 5600,
            "total_tool_use_tokens": None,
        }

    def model_dump(self):
        return dict(self._values)


class FakeInteraction:
    def __init__(self, text):
        self.output_text = text
        self.usage = FakeUsage()


class FakeFiles:
    def __init__(self, recorder):
        self.recorder = recorder

    def upload(self, file):
        self.recorder["uploads"].append(file)
        return FakeFile()

    def get(self, name):
        self.recorder["gets"].append(name)
        return FakeFile(name=name)


class FakeInteractions:
    def __init__(self, recorder):
        self.recorder = recorder

    def create(self, **kwargs):
        self.recorder["calls"].append(kwargs)
        return FakeInteraction('```json\n{"events": []}\n```')


class FakeClient:
    def __init__(self, recorder):
        self.files = FakeFiles(recorder)
        self.interactions = FakeInteractions(recorder)


@pytest.fixture
def gemini_recorder():
    return {"uploads": [], "gets": [], "calls": []}


@pytest.fixture
def fake_client(gemini_recorder):
    return FakeClient(gemini_recorder)


# --- gemini: リクエスト組み立て -------------------------------------------------


def test_gemini_image_blocks(tmp_path, fake_client, gemini_recorder):
    image = make_image(tmp_path)
    backend = GeminiBackend(client=fake_client, cache_dir=tmp_path / "uploads")

    response = backend.complete(LLMRequest(prompt="PROMPT", media=[MediaImage(path=image)]))

    call = gemini_recorder["calls"][0]
    assert call["model"] == "gemini-3.7-flash"
    assert "response_format" not in call
    blocks = call["input"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["mime_type"] == "image/jpeg"
    assert base64.b64decode(blocks[0]["data"]) == image.read_bytes()
    assert blocks[-1] == {"type": "text", "text": "PROMPT"}
    assert response.backend == "gemini"


def test_gemini_video_clip_processing_and_offsets(tmp_path, fake_client, gemini_recorder):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    backend = GeminiBackend(client=fake_client, cache_dir=tmp_path / "uploads")
    schema = {"type": "object"}

    backend.complete(
        LLMRequest(
            prompt="P",
            media=[MediaVideoClip(video=video, start_sec=12.0, end_sec=20.0, fps=5)],
            json_schema=schema,
        )
    )

    call = gemini_recorder["calls"][0]
    assert call["response_format"] == schema
    block = call["input"][0]
    assert block["type"] == "video"
    assert block["uri"].endswith("files/abc")
    assert block["mime_type"] == "video/mp4"
    assert block["processing"] == {
        "type": "static",
        "fps": 5,
        "start_offset": "12.00s",
        "end_offset": "20.00s",
    }
    assert block["resolution"] == "high"


def test_gemini_whole_video_omits_processing(tmp_path, fake_client, gemini_recorder):
    """音声解析などは動画全体を送る（processing を付けない）。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    backend = GeminiBackend(client=fake_client, cache_dir=tmp_path / "uploads")

    backend.complete(LLMRequest(prompt="音声について", media=[MediaVideoClip(video=video)]))

    block = gemini_recorder["calls"][0]["input"][0]
    assert "processing" not in block
    assert "resolution" not in block


def test_build_processing_and_offset_format(tmp_path):
    clip = MediaVideoClip(video=tmp_path / "v.mp4", start_sec=0.0, end_sec=8.0, fps=5)
    assert build_processing(clip)["start_offset"] == "0.00s"
    assert build_processing(MediaVideoClip(video=tmp_path / "v.mp4")) is None
    assert format_offset(48) == "48.00s"
    assert format_offset(1.234) == "1.23s"


# --- gemini: usage 正規化とアップロードキャッシュ -------------------------------


def test_gemini_normalizes_usage(tmp_path, fake_client):
    backend = GeminiBackend(client=fake_client, cache_dir=tmp_path / "uploads")
    response = backend.complete(LLMRequest(prompt="P", media=[]))

    assert response.usage["input_tokens"] == 5000
    assert response.usage["output_tokens"] == 400
    assert response.usage["reasoning_tokens"] == 200
    assert response.usage["total_tokens"] == 5600
    assert response.usage["cost_usd"] is None

    # 思考トークンは出力に含まれないので加算して概算する
    usd, is_estimate = estimate_cost_usd("gemini-3.7-flash", response.usage)
    assert is_estimate is True
    assert usd == pytest.approx(5000 * 0.75 / 1e6 + 600 * 3.75 / 1e6)


def test_gemini_upload_cache_is_reused(tmp_path, fake_client, gemini_recorder):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    cache_dir = tmp_path / "uploads"
    backend = GeminiBackend(client=fake_client, cache_dir=cache_dir)

    first = backend.upload(video)
    second = backend.upload(video)

    assert first == second
    assert len(gemini_recorder["uploads"]) == 1  # 2 回目はアップロードしない
    assert gemini_recorder["gets"] == ["files/abc"]  # ACTIVE 確認だけ行う
    assert len(list(cache_dir.glob("*.json"))) == 1


def test_gemini_supports_flags(tmp_path, fake_client):
    backend = GeminiBackend(client=fake_client, cache_dir=tmp_path)
    assert backend.supports_video_clip is True
    assert backend.supports_audio is True


def test_gemini_requires_google_genai(monkeypatch, tmp_path):
    """google-genai 未導入なら導入方法を案内して RuntimeError。"""
    monkeypatch.setitem(sys.modules, "google.genai", None)
    monkeypatch.setattr(
        "avs.backends.gemini._import_genai",
        lambda: (_ for _ in ()).throw(
            RuntimeError("gemini バックエンドには google-genai が必要です。`pip install google-genai`")
        ),
    )
    backend = GeminiBackend(cache_dir=tmp_path)
    with pytest.raises(RuntimeError, match="pip install google-genai"):
        _ = backend.client


def test_gemini_client_built_from_injected_sdk(monkeypatch, tmp_path):
    """sys.modules にダミー SDK を注入すれば遅延 import 経路も通る。"""
    created: dict = {}

    class DummyClient:
        def __init__(self, api_key):
            created["api_key"] = api_key

    google_module = types.ModuleType("google")
    genai_module = types.ModuleType("google.genai")
    genai_module.Client = DummyClient
    google_module.genai = genai_module
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)

    backend = GeminiBackend(api_key="gk-test", cache_dir=tmp_path)
    assert isinstance(backend.client, DummyClient)
    assert created["api_key"] == "gk-test"


# --- get_backend ---------------------------------------------------------------


def test_get_backend_returns_expected_types(tmp_path):
    assert get_backend("openrouter", api_key="k").name == "openrouter"
    assert get_backend("gemini", api_key="k", cache_dir=tmp_path).name == "gemini"
    assert get_backend("openrouter", model="x/y", api_key="k").model == "x/y"
    with pytest.raises(RuntimeError, match="未知のバックエンド"):
        get_backend("claude", api_key="k")


def test_describe_media(tmp_path):
    clip = MediaVideoClip(video=tmp_path / "v.mp4", start_sec=1.0, end_sec=2.5, fps=5)
    described = describe_media([MediaImage(path=tmp_path / "a.jpg"), clip])
    assert described[0] == "a.jpg"
    assert "v.mp4" in described[1] and "1.00s-2.50s" in described[1]
