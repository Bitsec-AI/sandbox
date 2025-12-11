FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://get.docker.com -o get-docker.sh && \
    sh get-docker.sh

WORKDIR /app

COPY requirements.txt config.py .
RUN pip install --no-cache-dir -r requirements.txt

COPY validator/ validator/
COPY loggers/ loggers/
COPY miner/ miner/
COPY neurons/ neurons/
COPY template/ template/

CMD ["python", "-m", "validator.manager"]
