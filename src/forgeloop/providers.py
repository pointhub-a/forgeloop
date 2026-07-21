"""Model provider abstractions for ForgeLoop."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
import json
import re
import socket
import time
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError

from forgeloop.models import Action


_FENCED_JSON = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```\Z",
    re.DOTALL,
)
_API_MESSAGE_ROLES = {"system", "user", "assistant"}


class ProviderError(RuntimeError):
    """Raised when a provider request or response cannot be used safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        http_status: int | None = None,
        attempts: int = 1,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.attempts = attempts
        self.retryable = retryable


class ProviderExhausted(ProviderError):
    """Raised when a scripted provider has no response left."""


class ActionParseError(ValueError):
    """Raised when a provider response is not exactly one valid action."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def parse_action(response: str) -> Action:
    """Parse exactly one strict JSON action, optionally wrapped in one fence."""

    try:
        document = response.strip()
        fenced = _FENCED_JSON.fullmatch(document)
        if fenced is not None:
            document = fenced.group("body")
        parsed = json.loads(
            document,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(parsed, dict):
            raise ValueError("action must be a JSON object")
        return Action.model_validate(parsed)
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise ActionParseError(
            "Model response was not a valid ForgeLoop action."
        ) from None


class Provider(Protocol):
    """Return one serialized action for a bounded conversation context."""

    def complete(
        self,
        messages: list[dict[str, str]],
        action_schema: dict[str, object],
    ) -> str: ...


def _post_json(
    *,
    base_url: str,
    api_key: str,
    timeout_seconds: int,
    payload: dict[str, object],
    opener: Callable[..., object],
) -> object:
    request = Request(
        base_url,
        data=json.dumps(payload, allow_nan=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with opener(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


class ScriptedProvider:
    """A deterministic provider used by tests and offline integrations."""

    def __init__(self, responses: Sequence[str]) -> None:
        self.responses = tuple(responses)
        self.calls: list[
            tuple[list[dict[str, str]], dict[str, object]]
        ] = []
        self._next_response = 0

    def complete(
        self,
        messages: list[dict[str, str]],
        action_schema: dict[str, object],
    ) -> str:
        self.calls.append((deepcopy(messages), deepcopy(action_schema)))
        if self._next_response >= len(self.responses):
            raise ProviderExhausted("scripted provider responses exhausted")
        response = self.responses[self._next_response]
        self._next_response += 1
        return response


class OpenAICompatibleProvider:
    """Call an OpenAI-compatible Chat Completions endpoint once per decision."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: int,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"model={self.model!r}, timeout_seconds={self.timeout_seconds!r}, "
            "api_key=<redacted>)"
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        action_schema: dict[str, object],
    ) -> str:
        normalized_messages = [
            self._normalize_message(message) for message in messages
        ]
        payload = {
            "model": self.model,
            "messages": normalized_messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "forgeloop_action",
                    "strict": True,
                    "schema": action_schema,
                },
            },
        }
        try:
            decoded = _post_json(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout_seconds=self.timeout_seconds,
                payload=payload,
                opener=self._opener,
            )
        except Exception:
            raise ProviderError("Provider request failed.") from None

        try:
            choices = decoded["choices"]
            if not isinstance(choices, list) or not choices:
                raise TypeError
            choice = choices[0]
            if not isinstance(choice, dict):
                raise TypeError
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError
        except (KeyError, IndexError, TypeError):
            raise ProviderError("Provider returned an invalid response.") from None
        return content

    @staticmethod
    def _normalize_message(message: dict[str, str]) -> dict[str, str]:
        role = message["role"]
        content = message["content"]
        if role in _API_MESSAGE_ROLES:
            return {"role": role, "content": content}
        return {"role": "user", "content": f"[{role}] {content}"}


class NewAPIProvider:
    """Call a New API Chat Completions endpoint using JSON-object mode."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: int,
        opener: Callable[..., object] = urlopen,
        sleeper: Callable[[float], object] = time.sleep,
        max_attempts: int = 2,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        if max_attempts not in {1, 2}:
            raise ValueError("max_attempts must be one or two")
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._opener = opener
        self._sleeper = sleeper
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"model={self.model!r}, timeout_seconds={self.timeout_seconds!r}, "
            "api_key=<redacted>)"
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        action_schema: dict[str, object],
    ) -> str:
        serialized_schema = json.dumps(
            action_schema,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        instruction = (
            "Return exactly one JSON object and no prose or Markdown. "
            "The object must satisfy this JSON Schema: "
            f"{serialized_schema}"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instruction},
                *[
                    OpenAICompatibleProvider._normalize_message(message)
                    for message in messages
                ],
            ],
            "response_format": {"type": "json_object"},
        }
        for attempt in range(1, self.max_attempts + 1):
            try:
                decoded = _post_json(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    timeout_seconds=self.timeout_seconds,
                    payload=payload,
                    opener=self._opener,
                )
                return self._extract_content(decoded, attempts=attempt)
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                error = self._safe_error(
                    code="http_error",
                    attempts=attempt,
                    retryable=retryable,
                    http_status=exc.code,
                )
            except (TimeoutError, socket.timeout):
                error = self._safe_error(
                    code="timeout",
                    attempts=attempt,
                    retryable=True,
                )
            except URLError:
                error = self._safe_error(
                    code="connection_error",
                    attempts=attempt,
                    retryable=True,
                )
            except OSError:
                error = self._safe_error(
                    code="connection_error",
                    attempts=attempt,
                    retryable=True,
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                error = self._safe_error(
                    code="invalid_response",
                    attempts=attempt,
                    retryable=False,
                )
            except ProviderError as exc:
                error = exc

            if not error.retryable or attempt == self.max_attempts:
                raise error from None
            self._sleeper(self.retry_delay_seconds)
        raise RuntimeError("unreachable")

    @staticmethod
    def _extract_content(decoded: object, *, attempts: int) -> str:
        try:
            if not isinstance(decoded, dict):
                raise TypeError
            choices = decoded["choices"]
            if not isinstance(choices, list) or not choices:
                raise TypeError
            choice = choices[0]
            if not isinstance(choice, dict):
                raise TypeError
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError
            if "content" not in message:
                raise KeyError
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise TypeError
            reasoning = message.get("reasoning_content")
            if reasoning is not None and not isinstance(reasoning, str):
                raise TypeError
        except (KeyError, IndexError, TypeError):
            raise NewAPIProvider._safe_error(
                code="invalid_response",
                attempts=attempts,
                retryable=False,
            ) from None

        if isinstance(content, str) and content.strip():
            return content
        if isinstance(reasoning, str):
            try:
                parse_action(reasoning)
            except ActionParseError:
                pass
            else:
                return reasoning
        raise NewAPIProvider._safe_error(
            code="empty_content",
            attempts=attempts,
            retryable=True,
        )

    @staticmethod
    def _safe_error(
        *,
        code: str,
        attempts: int,
        retryable: bool,
        http_status: int | None = None,
    ) -> ProviderError:
        attempt_word = "attempt" if attempts == 1 else "attempts"
        if code == "timeout":
            message = f"Provider timeout after {attempts} {attempt_word}."
        elif code == "connection_error":
            message = (
                f"Provider connection failed after {attempts} {attempt_word}."
            )
        elif code == "http_error":
            message = f"Provider returned HTTP {http_status}."
        elif code == "empty_content":
            message = (
                f"Provider returned empty content after {attempts} {attempt_word}."
            )
        else:
            message = "Provider returned an invalid response."
        return ProviderError(
            message,
            code=code,
            http_status=http_status,
            attempts=attempts,
            retryable=retryable,
        )
