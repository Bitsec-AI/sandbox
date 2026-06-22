FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    valkey-server \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY --from=loggers *.py loggers/

#CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["sh", "-c", "valkey-server --daemonize yes && uvicorn api:app --host 0.0.0.0 --port 8000 --workers 8"]
