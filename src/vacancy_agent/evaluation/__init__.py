"""Reproducible relevance-model evaluation (TASK-S2.4).

`dataset.py` loads/fingerprints the versioned golden dataset;
`prompt_fingerprint.py` fingerprints the eval-relevant prompt contract;
`records.py` defines the per-case run artifact schema; `manual_run.py` is
the online mode (calls a local LM Studio config, writes a resumable JSONL
artifact); `metrics.py`/`gate.py`/`compare.py` are fully offline — no
network, no LLM — and compare two already-produced artifacts against the
dataset and each other.
"""
