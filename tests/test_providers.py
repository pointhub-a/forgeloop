import json
from urllib.error import URLError

import pytest

from forgeloop.providers import (
    ActionParseError,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderExhausted,
    ScriptedProvider,
    parse_action,
)


def test_scripted_provider_records_feedback_messages():
    provider = ScriptedProvider(['{"kind":"run_validation","arguments":{}}'])

    provider.complete([{"role": "feedback", "content": "tests failed"}], {})

    assert provider.calls[0][0][-1]["role"] == "feedback"


def test_scripted_provider_fails_when_exhausted():
    with pytest.raises(ProviderExhausted):
        ScriptedProvider([]).complete([], {})


@pytest.mark.parametrize(
    "response",
    [
        '{"kind":"run_validation","arguments":{}}',
        '```json\n{"kind":"run_validation","arguments":{}}\n```',
        '```\n{"kind":"run_validation","arguments":{}}\n```',
    ],
)
def test_parse_action_accepts_one_json_object_with_optional_fence(response):
    parsed = parse_action(response)

    assert parsed.kind.value == "run_validation"
    assert parsed.arguments == {}


@pytest.mark.parametrize(
    "response",
    [
        '{"kind":"run_validation","arguments":{}} trailing',
        '[{"kind":"run_validation","arguments":{}}]',
        'prefix {"kind":"run_validation","arguments":{}}',
        '```json\n{"kind":"run_validation","arguments":{}}\n``` trailing',
        '{"kind":"run_validation","arguments":{},"extra":true}',
    ],
)
def test_parse_action_rejects_anything_except_one_strict_action(response):
    with pytest.raises(ActionParseError) as raised:
        parse_action(response)

    assert response not in str(raised.value)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_http_provider_sends_one_chat_completion_request_and_normalizes_roles():
    opened = []

    def opener(request, *, timeout):
        opened.append((request, timeout))
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"kind":"run_validation","arguments":{}}'
                        }
                    }
                ]
            }
        )

    provider = OpenAICompatibleProvider(
        "https://provider.example/v1/chat/completions",
        "test-model",
        "super-secret-key",
        17,
        opener=opener,
    )
    result = provider.complete(
        [
            {"role": "system", "content": "choose an action"},
            {"role": "feedback", "content": "tests failed"},
            {"role": "tool", "content": "file contents"},
        ],
        {"type": "object"},
    )

    assert result == '{"kind":"run_validation","arguments":{}}'
    assert len(opened) == 1
    request, timeout = opened[0]
    assert timeout == 17
    assert request.full_url == "https://provider.example/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer super-secret-key"
    body = json.loads(request.data)
    assert body == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "choose an action"},
            {"role": "user", "content": "[feedback] tests failed"},
            {"role": "user", "content": "[tool] file contents"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "forgeloop_action",
                "strict": True,
                "schema": {"type": "object"},
            },
        },
    }


def test_http_provider_redacts_api_key_from_repr_and_errors():
    api_key = "super-secret-key"

    def opener(request, *, timeout):
        raise URLError(f"failed while using {api_key}")

    provider = OpenAICompatibleProvider(
        "https://provider.example/v1/chat/completions",
        "test-model",
        api_key,
        17,
        opener=opener,
    )

    assert api_key not in repr(provider)
    with pytest.raises(ProviderError) as raised:
        provider.complete([], {})
    assert api_key not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
    ],
)
def test_http_provider_rejects_invalid_response_shape(payload):
    provider = OpenAICompatibleProvider(
        "https://provider.example/v1/chat/completions",
        "test-model",
        "secret",
        17,
        opener=lambda request, *, timeout: FakeResponse(payload),
    )

    with pytest.raises(ProviderError, match="invalid response"):
        provider.complete([], {})
