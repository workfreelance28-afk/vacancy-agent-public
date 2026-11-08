#!/bin/sh
set -eu

cleanup() {
    docker compose down --volumes --remove-orphans
}

verify_services() {
    curl --fail --silent --show-error \
        "http://127.0.0.1:${VACANCY_AGENT_API_PORT:-8000}/api/v1/health" >/dev/null
    curl --fail --silent --show-error \
        "http://127.0.0.1:${VACANCY_AGENT_API_PORT:-8000}/api/v1/ready" >/dev/null

    worker_id="$(docker compose ps --quiet worker)"
    test -n "$worker_id"
    test "$(docker inspect --format '{{.State.Running}}' "$worker_id")" = "true"
}

trap cleanup EXIT INT TERM

for run_number in 1 2; do
    cleanup
    docker compose up --build --detach --wait --wait-timeout 180
    verify_services
    echo "Compose smoke run ${run_number}/2 passed"
done
