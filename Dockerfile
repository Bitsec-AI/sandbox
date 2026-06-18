FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin:${PATH}"
ENV UV_FROZEN=1 UV_NO_DEV=1

RUN curl -fsSL https://get.docker.com -o get-docker.sh && \
    sh get-docker.sh

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY bitsec.py config.py version.py ./
COPY validator/ validator/
COPY loggers/ loggers/
COPY miner/ miner/
COPY neurons/ neurons/
COPY template/ template/

CMD ["uv", "run", "python", "-m", "validator.manager"]
