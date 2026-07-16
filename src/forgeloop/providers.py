"""Model provider abstractions for ForgeLoop."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
import json
import re
from typing import Callable, Protocol
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
            request = Request(
                self.base_url,
                data=json.dumps(payload, allow_nan=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self._opener(
                request, timeout=self.timeout_seconds
            ) as response:
                decoded = json.loads(response.read().decode("utf-8"))
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
