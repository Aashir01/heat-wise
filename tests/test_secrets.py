import json
from pathlib import Path

import pytest

from heatwise.secrets import REDACTED, get_secret, redact, redact_text, save_json

FAKE = "fg_test_1234567890abcdef"


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMP_API_KEY", FAKE)
    monkeypatch.setenv("NEBIUS_API_KEY", "nb_test_zzzzzzzzzz")


def test_redact_text_removes_key_and_bearer() -> None:
    out = redact_text(f"key={FAKE} Authorization: Bearer {FAKE}")
    assert FAKE not in out
    assert REDACTED in out


def test_redact_headers_and_nested() -> None:
    data = {"headers": {"api-key": FAKE, "Authorization": f"Bearer {FAKE}"}, "x": [FAKE]}
    out = json.dumps(redact(data))
    assert FAKE not in out


def test_signed_url_signature_removed() -> None:
    out = redact_text("https://s3/x.pdf?X-Amz-Signature=abc123&X-Amz-Expires=60")
    assert "abc123" not in out


def test_save_json_never_writes_secret(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    save_json(p, {"headers": {"api-key": FAKE}, "note": f"used {FAKE}"})
    assert FAKE not in p.read_text(encoding="utf-8")


def test_missing_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMP_API_KEY")
    with pytest.raises(KeyError):
        get_secret("TEMP_API_KEY")


def test_download_link_query_is_stripped() -> None:
    out = redact({"data": {"result": {"download_link": "https://s3/x.pdf?X-Foo=secret&Y=1"}}})
    assert "secret" not in json.dumps(out)
    assert out["data"]["result"]["download_link"].startswith("https://s3/x.pdf?")
