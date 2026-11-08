"""Manual eval run (TASK-S2.4) — the only online mode: calls one
explicitly-specified local LM Studio config against the golden dataset
and writes a resumable JSONL artifact (`EvalRecord` per line).

Never runs in CI (`docs/TASK-S2.4.md`: "CI запускает только offline gate
... без LM Studio"). Deliberately requires `--model`/`--base-url`
explicitly on the command line rather than falling back to
`QwenSettings`'s env-based defaults — a baseline/candidate artifact is
only meaningful if it is unambiguous which local config produced it.
"""

import argparse
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from vacancy_agent.config.settings import QwenSettings
from vacancy_agent.domain.qwen_errors import QwenClientError
from vacancy_agent.evaluation.dataset import GoldenCase, dataset_fingerprint, load_dataset
from vacancy_agent.evaluation.prompt_fingerprint import prompt_fingerprint
from vacancy_agent.evaluation.records import (
    EvalCost,
    EvalRecord,
    EvalUsage,
    predicted_class_from_assessment,
)
from vacancy_agent.integrations.qwen_client import LMStudioQwenClient
from vacancy_agent.integrations.qwen_prompt import SCHEMA_VERSION

logger = logging.getLogger("vacancy_agent.evaluation.manual_run")

_TEMPERATURE = 0.0


class IncompatibleResumeError(Exception):
    """`--output` already has records from a different dataset/prompt/model
    — resuming would silently mix incompatible runs into one artifact."""


def run(
    *,
    dataset_path: Path,
    output_path: Path,
    model: str,
    base_url: str,
    api_key: str,
    request_timeout_seconds: float,
    max_retries: int,
    retry_backoff_seconds: float,
    limit: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    all_cases = load_dataset(dataset_path)
    cases = all_cases[:limit] if limit is not None else all_cases
    current_fingerprint = dataset_fingerprint(dataset_path)
    current_prompt_fingerprint = prompt_fingerprint()

    existing = _load_existing_records(
        output_path,
        dataset_fingerprint=current_fingerprint,
        prompt_fingerprint=current_prompt_fingerprint,
        model=model,
    )
    done_ids = {
        case_id for case_id, record in existing.items() if record.predicted_class is not None
    }
    pending = [case for case in cases if case.case_id not in done_ids]
    logger.info(
        "manual run: %d cases total, %d already done, %d pending",
        len(cases),
        len(done_ids),
        len(pending),
    )

    settings = QwenSettings(
        base_url=base_url,
        model=model,
        api_key=SecretStr(api_key),
        request_timeout_seconds=request_timeout_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    records_by_id = dict(existing)
    with LMStudioQwenClient(settings, transport=transport) as client:
        for case in pending:
            record = _run_one(
                client,
                case,
                dataset_fingerprint=current_fingerprint,
                prompt_fingerprint=current_prompt_fingerprint,
                base_url=base_url,
            )
            # Upsert-by-case_id, not append: a case that previously errored
            # already has a stale record in `existing` for the same
            # `case_id` — appending the retry's result alongside it would
            # leave the artifact with two conflicting rows for one
            # `case_id`, which `compare._records_by_case_id()` rejects as
            # a duplicate. The whole file is rewritten (atomically, via a
            # temp file + rename) after every case so a crash mid-run
            # still leaves a valid, compare-able artifact — the failed
            # attempt itself is still visible in this run's log output.
            records_by_id[case.case_id] = record
            _write_records(output_path, all_cases, records_by_id)
            outcome = record.predicted_class or f"ERROR: {record.error}"
            logger.info("case=%s -> %s", case.case_id, outcome)


def _run_one(
    client: LMStudioQwenClient,
    case: GoldenCase,
    *,
    dataset_fingerprint: str,
    prompt_fingerprint: str,
    base_url: str,
) -> EvalRecord:
    created_at = datetime.now(UTC)
    try:
        outcome = client.assess_with_metadata(case.vacancy)
    except QwenClientError as exc:
        return EvalRecord(
            case_id=case.case_id,
            dataset_fingerprint=dataset_fingerprint,
            prompt_fingerprint=prompt_fingerprint,
            schema_version=SCHEMA_VERSION,
            model=client.model,
            base_url=base_url,
            temperature=_TEMPERATURE,
            created_at=created_at,
            elapsed_seconds=0.0,
            usage=EvalUsage(),
            cost=EvalCost.local_lm_studio(),
            predicted_class=None,
            assessment=None,
            error=type(exc).__name__,
        )
    return EvalRecord(
        case_id=case.case_id,
        dataset_fingerprint=dataset_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        schema_version=SCHEMA_VERSION,
        model=client.model,
        base_url=base_url,
        temperature=_TEMPERATURE,
        created_at=created_at,
        elapsed_seconds=outcome.elapsed_seconds,
        usage=EvalUsage.from_payload(outcome.usage),
        cost=EvalCost.local_lm_studio(),
        predicted_class=predicted_class_from_assessment(outcome.assessment),
        assessment=outcome.assessment,
        error=None,
    )


def _load_existing_records(
    output_path: Path, *, dataset_fingerprint: str, prompt_fingerprint: str, model: str
) -> dict[str, EvalRecord]:
    """Every record already in `--output`, keyed by `case_id` — including
    cases that previously errored (`run()` retries those, not skips them:
    TASK-S2.4 "сбой одного кейса виден и не теряет уже готовые" — a
    transient failure shouldn't permanently block that case from ever
    getting a real result). Kept, not just the successful ones, so `run()`
    can upsert into the same in-memory map and rewrite a duplicate-free
    file after every case."""

    if not output_path.exists():
        return {}

    existing: dict[str, EvalRecord] = {}
    for line in output_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = EvalRecord.model_validate(json.loads(line))
        if record.dataset_fingerprint != dataset_fingerprint:
            raise IncompatibleResumeError(
                f"{output_path} already has records for a different dataset "
                f"(expected fingerprint {dataset_fingerprint[:12]}…, "
                f"found {record.dataset_fingerprint[:12]}…) — use a new --output file"
            )
        if record.prompt_fingerprint != prompt_fingerprint:
            raise IncompatibleResumeError(
                f"{output_path} already has records for a different prompt contract "
                f"(expected {prompt_fingerprint[:12]}…, found {record.prompt_fingerprint[:12]}…) "
                "— use a new --output file"
            )
        if record.model != model:
            raise IncompatibleResumeError(
                f"{output_path} already has records for model {record.model!r}, "
                f"not {model!r} — use a new --output file"
            )
        existing[record.case_id] = record
    return existing


def _write_records(
    output_path: Path, ordered_cases: list[GoldenCase], records_by_id: dict[str, EvalRecord]
) -> None:
    """Rewrites the whole artifact — in dataset order, one line per
    `case_id`, at most one line per `case_id` — instead of appending. A
    temp file + `os.replace` keeps this atomic: a crash mid-write leaves
    either the old, still-valid file or the new one, never a truncated
    one, so a resumed run always starts from a well-formed artifact."""

    lines = [
        records_by_id[case.case_id].model_dump_json()
        for case in ordered_cases
        if case.case_id in records_by_id
    ]
    fd, tmp_name = tempfile.mkstemp(
        dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            for line in lines:
                tmp_file.write(line + "\n")
        os.replace(tmp_name, output_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual eval run against one explicit local LM Studio config."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/evaluation/vacancy_relevance_golden_v1.jsonl"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Exact LM Studio model identifier.")
    parser.add_argument("--base-url", default="http://localhost:1234/v1")
    parser.add_argument("--api-key", default="lm-studio")
    parser.add_argument("--request-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--retry-backoff-seconds", type=float, default=1.0)
    parser.add_argument(
        "--limit", type=int, default=None, help="Only run the first N cases (smoke-testing)."
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_parser().parse_args()
    run(
        dataset_path=args.dataset,
        output_path=args.output,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        request_timeout_seconds=args.request_timeout_seconds,
        max_retries=args.max_retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
