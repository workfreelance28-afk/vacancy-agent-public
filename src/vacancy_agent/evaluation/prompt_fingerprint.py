"""Fingerprint of the eval-relevant prompt contract (TASK-S2.4).

Covers everything that changes what the model is actually asked and how
its answer is validated: the system prompt text, the structured-output
JSON schema, `VacancyAssessment`'s schema version, the source of
`build_user_message` (plus the helpers it calls, `_truncate`/
`_format_salary`), and `_MAX_DESCRIPTION_CHARS` — the per-vacancy user
message is built dynamically from a `Vacancy`, so it can't be hashed as a
fixed string the way `SYSTEM_PROMPT` can. Hashing the functions' source
covers changes to field selection or *how* truncation works; the
truncation *limit* is a plain module-level constant referenced by name in
that source, so its value doesn't show up in `inspect.getsource()` at all
and is folded into the payload separately (read live via the `qwen_prompt`
module attribute, not imported by value, so a change to it — including a
test's `monkeypatch.setattr(qwen_prompt, "_MAX_DESCRIPTION_CHARS", ...)`
— is actually picked up). Otherwise a resumed run could silently mix
records built from two different actual prompts.

Baseline/candidate runs each record this fingerprint — `compare.py` uses
it only to report whether they used the same contract or a deliberately
different one; it does not gate on a mismatch (changing prompt/model is
explicitly out of scope for this TASK, but a future prompt-tuning run
should still be comparable and clearly labelled as such)."""

import hashlib
import inspect
import json

import vacancy_agent.integrations.qwen_prompt as qwen_prompt
from vacancy_agent.integrations.qwen_prompt import (
    RESPONSE_JSON_SCHEMA,
    SCHEMA_VERSION,
    SYSTEM_PROMPT,
    _format_salary,
    _truncate,
    build_user_message,
)


def prompt_fingerprint() -> str:
    payload = json.dumps(
        {
            "system_prompt": SYSTEM_PROMPT,
            "response_schema": RESPONSE_JSON_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "user_message_source": _user_message_source(),
            "max_description_chars": qwen_prompt._MAX_DESCRIPTION_CHARS,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _user_message_source() -> str:
    return "\n".join(
        inspect.getsource(func) for func in (build_user_message, _truncate, _format_salary)
    )
