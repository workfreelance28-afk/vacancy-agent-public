"""Regression tests, TASK-S2.4 review-fix: `prompt_fingerprint()` must
change when the actual user-message formation (`build_user_message` and
the helpers it calls) changes, not only when `SYSTEM_PROMPT`/
`RESPONSE_JSON_SCHEMA`/`SCHEMA_VERSION` do — otherwise a real prompt
change (e.g. a longer truncation limit, a new field sent to the model)
would not invalidate a resumed run's fingerprint check."""

import pytest

import vacancy_agent.evaluation.prompt_fingerprint as prompt_fingerprint_module
import vacancy_agent.integrations.qwen_prompt as qwen_prompt
from vacancy_agent.evaluation.prompt_fingerprint import prompt_fingerprint


def test_prompt_fingerprint_is_stable_when_nothing_changed() -> None:
    assert prompt_fingerprint() == prompt_fingerprint()


def test_prompt_fingerprint_changes_when_truncate_helper_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = prompt_fingerprint()

    def patched_truncate(text: str | None) -> str | None:
        # Same signature/behavior-ish, different source text — enough to
        # simulate "someone changed the truncation rule".
        if text is None:
            return None
        return text[:1] + "…"

    # `prompt_fingerprint.py` does `from ...qwen_prompt import _truncate`,
    # so it holds its own name binding — patching `qwen_prompt._truncate`
    # would not reach it (classic "patch where it's used"). This mirrors
    # what a real code edit to `qwen_prompt._truncate` looks like: a new
    # function object under the same name.
    monkeypatch.setattr(prompt_fingerprint_module, "_truncate", patched_truncate)

    assert prompt_fingerprint() != before


def test_prompt_fingerprint_changes_when_build_user_message_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = prompt_fingerprint()

    def patched_build_user_message(vacancy: object) -> str:
        return "different implementation entirely"

    monkeypatch.setattr(
        prompt_fingerprint_module, "build_user_message", patched_build_user_message
    )

    assert prompt_fingerprint() != before


def test_prompt_fingerprint_changes_when_max_description_chars_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_MAX_DESCRIPTION_CHARS` is a plain module-level constant referenced
    by name inside `_truncate`'s source — its *value* never shows up in
    `inspect.getsource(_truncate)`, so hashing only the functions' source
    would silently miss a change to the truncation limit itself.
    `prompt_fingerprint()` reads it live via the `qwen_prompt` module
    attribute (not imported by value), so patching it here — the same way
    a real config/constant change would look at runtime — must change the
    fingerprint."""

    before = prompt_fingerprint()

    monkeypatch.setattr(qwen_prompt, "_MAX_DESCRIPTION_CHARS", 999999)

    assert prompt_fingerprint() != before
