"""
LLM provider abstraction.

Why this exists now (and didn't from the start): early in this project
we deliberately avoided a provider abstraction -- with a single
concrete implementation (OpenAI), an interface would have been
speculative complexity with no real payoff (YAGNI). That calculus
changes the moment we have a second concrete need with real behavioral
requirements: automatic failover from OpenAI to Gemini when one is
unavailable (e.g. quota exhausted). Two concrete implementations behind
one contract is exactly the point where an abstraction starts paying
for itself instead of just adding indirection.

Each provider's `generate()` is expected to raise on any failure
(auth error, quota, timeout, network error, etc.) -- callers in llm.py
catch broadly and move to the next provider in the list. This mirrors
the same "catch broadly, one failure doesn't stop the pipeline"
philosophy used everywhere else in this project.
"""

from __future__ import annotations

from typing import Protocol


class LLMProvider(Protocol):
    """Minimal contract every LLM provider must satisfy."""

    name: str

    def generate(self, system_prompt: str, user_prompt: str, timeout: float) -> str:
        """Returns the raw generated text. Must raise on any failure."""
        ...


class OpenAIProvider:
    """Wraps an OpenAI client (already-constructed, API key included)."""

    name = "openai"

    def __init__(self, client, model: str):
        self._client = client
        self._model = model

    def generate(self, system_prompt: str, user_prompt: str, timeout: float) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            timeout=timeout,
        )
        return (response.choices[0].message.content or "").strip()


class GeminiProvider:
    """
    Wraps a Gemini client from the official `google-genai` SDK.

    Note on timeout: unlike the OpenAI SDK, `google-genai` does not
    expose a simple per-call `timeout` kwarg on `generate_content` --
    timeouts are configured via `types.HttpOptions` at client
    construction time. This means the `timeout` argument passed to
    `generate()` is currently NOT enforced per-call for Gemini; it's
    accepted for interface symmetry with OpenAIProvider but the actual
    timeout in effect is whatever the client was built with (or the
    SDK's default). Flagged here explicitly rather than silently
    ignored -- see README's production improvements section.
    """

    name = "gemini"

    def __init__(self, client, model: str):
        self._client = client
        self._model = model

    def generate(self, system_prompt: str, user_prompt: str, timeout: float) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.3,
            ),
        )
        return (response.text or "").strip()
