FROM python:3.11-slim

LABEL org.opencontainers.image.source=https://github.com/Bitsec-AI/sandbox

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY run_sandbox.py ./

CMD ["python", "run_sandbox.py"]
