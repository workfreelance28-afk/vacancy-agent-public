FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 vacancy-agent \
    && useradd --uid 10001 --gid vacancy-agent --create-home vacancy-agent

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install .

USER vacancy-agent

EXPOSE 8000
STOPSIGNAL SIGINT

CMD ["vacancy-agent-api"]
