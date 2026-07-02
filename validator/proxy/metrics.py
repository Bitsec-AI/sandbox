import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any

from loggers.logger import get_logger

from models import LLMAttemptMetrics, ProxyMetricsContext, ProxySummaryRow


logger = get_logger()

METRICS_TTL_SECONDS = 40 * 60
KEY_PREFIX = "proxy_metrics:v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_id_for(phase: str, req_model: str, model: str, provider: str) -> str:
    raw = f"{phase}\0{req_model}\0{model}\0{provider}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _row_has_counts(row: ProxySummaryRow) -> bool:
    return any(
        getattr(row, field) != 0
        for field in (
            "requests",
            "success",
            "error",
            "llm_requests",
            "llm_success",
            "llm_error",
            "retries",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "duration_ms_total",
            "duration_ms_max",
        )
    ) or bool(row.status_codes)


class ProxyMetricsRecorder:
    def __init__(self, client: Any | None = None):
        self.client = client if client is not None else self._init_client()

    def _init_client(self):
        try:
            try:
                import valkey
            except ImportError:
                import redis as valkey

            client_cls = getattr(valkey, "Valkey", None) or getattr(valkey, "Redis")
            return client_cls(
                host=os.getenv("VALKEY_HOST", "127.0.0.1"),
                port=int(os.getenv("VALKEY_PORT", "6379")),
                db=int(os.getenv("VALKEY_DB", "0")),
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.5,
            )
        except Exception as e:
            logger.warning(f"Proxy metrics disabled; Valkey client unavailable: {e}")
            return None

    def _keys(self, job_run_id: str) -> dict[str, str]:
        base = f"{KEY_PREFIX}:job_run:{job_run_id}"
        return {
            "meta": f"{base}:meta",
            "rows": f"{base}:rows",
            "stream": f"{base}:llm_stream",
            "row_prefix": f"{base}:row:",
        }

    def _row_key(self, job_run_id: str, row_id: str) -> str:
        return f"{self._keys(job_run_id)['row_prefix']}{row_id}"

    def _touch(self, pipe: Any, *keys: str) -> None:
        for key in keys:
            pipe.expire(key, METRICS_TTL_SECONDS)

    def _ensure_row(self, pipe: Any, ctx: ProxyMetricsContext) -> tuple[str, str, str]:
        now = utc_now()
        keys = self._keys(ctx.job_run_id)
        row_id = row_id_for(ctx.phase, ctx.req_model, ctx.model, ctx.provider)
        row_key = self._row_key(ctx.job_run_id, row_id)

        pipe.hset(
            keys["meta"],
            mapping={
                "job_run_id": ctx.job_run_id,
                "updated_at": now,
            },
        )
        pipe.hsetnx(keys["meta"], "created_at", now)
        pipe.sadd(keys["rows"], row_id)
        pipe.hset(
            row_key,
            mapping={
                "phase": ctx.phase,
                "req_model": ctx.req_model,
                "model": ctx.model,
                "provider": ctx.provider,
            },
        )
        self._touch(pipe, keys["meta"], keys["rows"], row_key, keys["stream"])
        return keys["stream"], row_key, row_id

    def _execute(self, callback) -> None:
        if self.client is None:
            return
        try:
            callback()
        except Exception as e:
            logger.warning(f"Proxy metrics update skipped: {e}")

    def record_proxy_complete(self, ctx: ProxyMetricsContext, *, success: bool, duration_ms: int) -> None:
        def _record():
            row_id = row_id_for(ctx.phase, ctx.req_model, ctx.model, ctx.provider)
            row_key = self._row_key(ctx.job_run_id, row_id)
            current_max = self.client.hget(row_key, "duration_ms_max")
            pipe = self.client.pipeline()
            self._ensure_row(pipe, ctx)
            pipe.hincrby(row_key, "requests", 1)
            pipe.hincrby(row_key, "success" if success else "error", 1)
            pipe.hincrby(row_key, "duration_ms_total", int(duration_ms))
            if int(duration_ms) > int(current_max or 0):
                pipe.hset(row_key, "duration_ms_max", int(duration_ms))
            pipe.execute()

        self._execute(_record)

    def record_llm_attempt(self, ctx: ProxyMetricsContext, attempt: LLMAttemptMetrics) -> None:
        def _record():
            pipe = self.client.pipeline()
            stream_key, row_key, _ = self._ensure_row(pipe, ctx)

            is_success = attempt.error_type == "" and 200 <= int(attempt.status_code) < 400
            pipe.hincrby(row_key, "llm_requests", 1)
            pipe.hincrby(row_key, "llm_success" if is_success else "llm_error", 1)
            if int(attempt.retry_num) > 0:
                pipe.hincrby(row_key, "retries", 1)
            pipe.hincrby(row_key, f"status_code:{int(attempt.status_code)}", 1)
            pipe.hincrby(row_key, "input_tokens", int(attempt.input_tokens))
            pipe.hincrby(row_key, "output_tokens", int(attempt.output_tokens))
            pipe.hincrby(row_key, "cached_tokens", int(attempt.cached_tokens))
            pipe.xadd(
                stream_key,
                {
                    "job_run_id": ctx.job_run_id,
                    "phase": ctx.phase,
                    "req_model": ctx.req_model,
                    "model": ctx.model,
                    "provider": ctx.provider,
                    "upstream_provider": attempt.upstream_provider,
                    "status_code": str(int(attempt.status_code)),
                    "duration_ms": str(int(attempt.duration_ms)),
                    "input_tokens": str(int(attempt.input_tokens)),
                    "output_tokens": str(int(attempt.output_tokens)),
                    "cached_tokens": str(int(attempt.cached_tokens)),
                    "retry_num": str(int(attempt.retry_num)),
                    "finish_reason": attempt.finish_reason,
                    "error_type": attempt.error_type,
                },
            )
            pipe.expire(stream_key, METRICS_TTL_SECONDS)
            pipe.execute()

        self._execute(_record)

    def reset_summary(self, job_run_id: str) -> None:
        if self.client is None:
            return
        try:
            keys = self._keys(job_run_id)
            row_ids = self.client.smembers(keys["rows"]) or set()
            delete_keys = [keys["meta"], keys["rows"], keys["stream"]]
            delete_keys.extend(self._row_key(job_run_id, row_id) for row_id in row_ids)
            if delete_keys:
                self.client.delete(*delete_keys)
        except Exception as e:
            logger.warning(f"Proxy metrics reset skipped: {e}")

    def get_summary(self, job_run_id: str) -> dict[str, Any]:
        generated_at = utc_now()
        if self.client is None:
            return {"job_run_id": job_run_id, "generated_at": generated_at, "stats": []}

        try:
            keys = self._keys(job_run_id)
            meta = self.client.hgetall(keys["meta"]) or {}
            rows = []
            for row_id in sorted(self.client.smembers(keys["rows"]) or []):
                row = self.client.hgetall(self._row_key(job_run_id, row_id)) or {}
                if not row:
                    continue
                status_codes = {}
                payload = {}
                for key, value in row.items():
                    if key.startswith("status_code:"):
                        status_codes[key.split(":", 1)[1]] = int(value)
                    elif key in {"phase", "req_model", "model", "provider"}:
                        payload[key] = value
                    else:
                        payload[key] = int(value)

                payload["status_codes"] = status_codes
                summary_row = ProxySummaryRow.model_validate(payload)
                if not _row_has_counts(summary_row):
                    continue
                rows.append(summary_row.to_summary_dict())

            return {
                "job_run_id": meta.get("job_run_id", job_run_id),
                "generated_at": generated_at,
                "stats": rows,
            }
        except Exception as e:
            logger.warning(f"Proxy metrics summary unavailable: {e}")
            return {"job_run_id": job_run_id, "generated_at": generated_at, "stats": []}


_recorder = ProxyMetricsRecorder()


def get_metrics_recorder() -> ProxyMetricsRecorder:
    return _recorder


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)
