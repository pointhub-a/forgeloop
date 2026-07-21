import json
from io import BytesIO
import socket
from urllib.error import HTTPError, URLError

import pytest

import forgeloop.providers as provider_module

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


class RawResponse(FakeResponse):
    def read(self):
        return self.payload


class QueuedOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


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
        "unmistakably-fake-provider-key",
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
    assert (
        request.get_header("Authorization")
        == "Bearer unmistakably-fake-provider-key"
    )
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


def test_newapi_provider_sends_json_object_request_with_schema_instruction():
    opened = []
    sleeps = []

    def opener(request, *, timeout):
        opened.append((request, timeout))
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"kind":"run_validation","arguments":{}}'
                            )
                        }
                    }
                ]
            }
        )

    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "unmistakably-fake-provider-key",
        20,
        opener=opener,
        sleeper=sleeps.append,
    )

    result = provider.complete(
        [{"role": "feedback", "content": "validation failed"}],
        {"type": "object", "required": ["kind", "arguments"]},
    )

    assert result == '{"kind":"run_validation","arguments":{}}'
    assert len(opened) == 1
    request, timeout = opened[0]
    assert timeout == 20
    body = json.loads(request.data)
    assert body["model"] == "qwen-turbo"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert "Return exactly one JSON object" in body["messages"][0]["content"]
    assert '"required":["kind","arguments"]' in body["messages"][0]["content"]
    assert body["messages"][1] == {
        "role": "user",
        "content": "[feedback] validation failed",
    }
    assert sleeps == []


def test_newapi_provider_serializes_schema_instruction_deterministically():
    opened = []

    def opener(request, *, timeout):
        opened.append(request)
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"kind":"run_validation","arguments":{}}'
                            )
                        }
                    }
                ]
            }
        )

    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "fake-key",
        20,
        opener=opener,
    )

    provider.complete([], {"required": ["kind"], "type": "object"})
    provider.complete([], {"type": "object", "required": ["kind"]})

    instructions = [
        json.loads(request.data)["messages"][0]["content"] for request in opened
    ]
    assert instructions[0] == instructions[1]


def test_newapi_provider_prefers_nonempty_content_over_reasoning():
    content = "not yet a valid action"
    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "fake-key",
        20,
        opener=lambda request, *, timeout: FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": content,
                            "reasoning_content": (
                                '{"kind":"run_validation","arguments":{}}'
                            ),
                        }
                    }
                ]
            }
        ),
    )

    assert provider.complete([], {}) == content


def test_newapi_provider_accepts_only_valid_reasoning_when_content_is_empty():
    reasoning = '{"kind":"run_validation","arguments":{}}'
    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "fake-key",
        20,
        opener=lambda request, *, timeout: FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "  ",
                            "reasoning_content": reasoning,
                        }
                    }
                ]
            }
        ),
    )

    assert provider.complete([], {}) == reasoning


def test_newapi_provider_retries_empty_content_without_exposing_reasoning():
    sensitive_reasoning = "private model reasoning that is not an action"
    opener = QueuedOpener(
        [
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": sensitive_reasoning,
                                "tool_calls": [{"function": {"name": "shell"}}],
                            }
                        }
                    ]
                }
            ),
            FakeResponse({"choices": [{"message": {"content": None}}]}),
        ]
    )
    sleeps = []
    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "fake-key",
        20,
        opener=opener,
        sleeper=sleeps.append,
    )

    with pytest.raises(ProviderError) as raised:
        provider.complete([], {})

    assert raised.value.code == "empty_content"
    assert raised.value.attempts == 2
    assert raised.value.retryable is True
    assert sensitive_reasoning not in str(raised.value)
    assert len(opener.calls) == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize("failure", [TimeoutError(), socket.timeout()])
def test_newapi_provider_retries_timeouts(failure):
    opener = QueuedOpener(
        [
            failure,
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"kind":"run_validation","arguments":{}}'
                                )
                            }
                        }
                    ]
                }
            ),
        ]
    )
    sleeps = []
    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "fake-key",
        20,
        opener=opener,
        sleeper=sleeps.append,
    )

    assert parse_action(provider.complete([], {})).kind.value == "run_validation"
    assert len(opener.calls) == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    "failure",
    [
        URLError("connection unavailable"),
        ConnectionError("connection reset"),
        HTTPError(
            "https://provider.example",
            429,
            "rate limited",
            {},
            BytesIO(b"upstream-private-body"),
        ),
        HTTPError(
            "https://provider.example",
            503,
            "unavailable",
            {},
            BytesIO(b"upstream-private-body"),
        ),
    ],
)
def test_newapi_provider_retries_transient_transport_failures(failure):
    opener = QueuedOpener(
        [
            failure,
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"kind":"run_validation","arguments":{}}'
                                )
                            }
                        }
                    ]
                }
            ),
        ]
    )
    sleeps = []
    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "fake-key",
        20,
        opener=opener,
        sleeper=sleeps.append,
    )

    assert parse_action(provider.complete([], {})).kind.value == "run_validation"
    assert len(opener.calls) == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_newapi_provider_does_not_retry_terminal_http_errors(status):
    api_key = "unmistakably-fake-provider-key"
    upstream_body = b"upstream-private-body"
    opener = QueuedOpener(
        [
            HTTPError(
                "https://provider.example",
                status,
                "rejected",
                {},
                BytesIO(upstream_body),
            )
        ]
    )
    sleeps = []
    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        api_key,
        20,
        opener=opener,
        sleeper=sleeps.append,
    )

    with pytest.raises(ProviderError) as raised:
        provider.complete([], {})

    assert raised.value.code == "http_error"
    assert raised.value.http_status == status
    assert raised.value.attempts == 1
    assert raised.value.retryable is False
    assert api_key not in repr(provider)
    assert api_key not in str(raised.value)
    assert upstream_body.decode() not in str(raised.value)
    assert len(opener.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "response",
    [
        RawResponse(b"not-json"),
        FakeResponse({}),
        FakeResponse({"choices": []}),
        FakeResponse({"choices": [{}]}),
        FakeResponse({"choices": [{"message": {}}]}),
        FakeResponse({"choices": [{"message": "wrong-type"}]}),
    ],
)
def test_newapi_provider_does_not_retry_invalid_responses(response):
    opener = QueuedOpener([response])
    sleeps = []
    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "fake-key",
        20,
        opener=opener,
        sleeper=sleeps.append,
    )

    with pytest.raises(ProviderError) as raised:
        provider.complete([], {})

    assert raised.value.code == "invalid_response"
    assert raised.value.attempts == 1
    assert raised.value.retryable is False
    assert len(opener.calls) == 1
    assert sleeps == []


def test_newapi_provider_reports_terminal_retryable_failure_safely():
    secret = "transport-secret-that-must-not-leak"
    opener = QueuedOpener([URLError(secret), URLError(secret)])
    provider = provider_module.NewAPIProvider(
        "https://provider.example/v1/chat/completions",
        "qwen-turbo",
        "fake-key",
        20,
        opener=opener,
        sleeper=lambda delay: None,
    )

    with pytest.raises(ProviderError) as raised:
        provider.complete([], {})

    assert raised.value.code == "connection_error"
    assert raised.value.attempts == 2
    assert raised.value.retryable is True
    assert secret not in str(raised.value)


@pytest.mark.parametrize("max_attempts", [0, 3])
def test_newapi_provider_rejects_attempt_budgets_outside_one_or_two(
    max_attempts,
):
    with pytest.raises(ValueError, match="max_attempts"):
        provider_module.NewAPIProvider(
            "https://provider.example/v1/chat/completions",
            "qwen-turbo",
            "fake-key",
            20,
            max_attempts=max_attempts,
        )


def test_http_provider_redacts_api_key_from_repr_and_errors():
    api_key = "unmistakably-fake-provider-key"

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
