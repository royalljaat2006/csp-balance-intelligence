"""
autopilot.nemotron_client — the one seam every autopilot feature calls
through. No real network calls: OpenAI's client is mocked entirely, since
this is pure request-shaping/response-parsing logic.
"""

from unittest.mock import MagicMock, patch

import pytest
from autopilot.nemotron_client import NemotronUnavailable, chat_json
from django.test import override_settings


def _fake_response(content: str):
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


@override_settings(NEMOTRON_API_KEY="")
def test_chat_json_raises_without_api_key():
    with pytest.raises(NemotronUnavailable):
        chat_json("system", "user")


@override_settings(NEMOTRON_API_KEY="fake-key", NEMOTRON_MODEL="fake-model")
@patch("autopilot.nemotron_client.OpenAI")
def test_chat_json_parses_clean_json(mock_openai_cls):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _fake_response('{"headline": "ok"}')
    mock_openai_cls.return_value = mock_client

    result = chat_json("system", "user")

    assert result == {"headline": "ok"}
    mock_client.chat.completions.create.assert_called_once()
    _, kwargs = mock_client.chat.completions.create.call_args
    assert kwargs["model"] == "fake-model"


@override_settings(NEMOTRON_API_KEY="fake-key", NEMOTRON_MODEL="fake-model")
@patch("autopilot.nemotron_client.OpenAI")
def test_chat_json_strips_markdown_fence(mock_openai_cls):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _fake_response(
        '```json\n{"headline": "fenced"}\n```'
    )
    mock_openai_cls.return_value = mock_client

    assert chat_json("system", "user") == {"headline": "fenced"}


@override_settings(NEMOTRON_API_KEY="fake-key", NEMOTRON_MODEL="fake-model")
@patch("autopilot.nemotron_client.OpenAI")
def test_chat_json_raises_on_invalid_json(mock_openai_cls):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _fake_response("not json at all")
    mock_openai_cls.return_value = mock_client

    with pytest.raises(NemotronUnavailable):
        chat_json("system", "user")


@override_settings(NEMOTRON_API_KEY="fake-key", NEMOTRON_MODEL="fake-model")
@patch("autopilot.nemotron_client.OpenAI")
def test_chat_json_raises_on_client_error(mock_openai_cls):
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("network blip")
    mock_openai_cls.return_value = mock_client

    with pytest.raises(NemotronUnavailable):
        chat_json("system", "user")
