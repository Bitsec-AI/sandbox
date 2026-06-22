from validator.proxy.metrics import (
    LLMAttemptMetrics,
    METRICS_TTL_SECONDS,
    ProxyMetricsContext,
    ProxyMetricsRecorder,
)


class FakeValkey:
    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.streams = {}
        self.ttls = {}

    def pipeline(self):
        return self

    def execute(self):
        return True

    def hset(self, key, field=None, value=None, mapping=None):
        self.hashes.setdefault(key, {})
        if mapping is not None:
            self.hashes[key].update({str(k): str(v) for k, v in mapping.items()})
            return len(mapping)
        self.hashes[key][str(field)] = str(value)
        return 1

    def hsetnx(self, key, field, value):
        self.hashes.setdefault(key, {})
        if field in self.hashes[key]:
            return 0
        self.hashes[key][str(field)] = str(value)
        return 1

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hincrby(self, key, field, amount):
        self.hashes.setdefault(key, {})
        self.hashes[key][field] = str(int(self.hashes[key].get(field, 0)) + int(amount))
        return int(self.hashes[key][field])

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)
        return 1

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def xadd(self, key, fields):
        self.streams.setdefault(key, []).append({str(k): str(v) for k, v in fields.items()})
        return f"{len(self.streams[key])}-0"

    def expire(self, key, seconds):
        self.ttls[key] = seconds
        return 1

    def delete(self, *keys):
        for key in keys:
            self.hashes.pop(key, None)
            self.sets.pop(key, None)
            self.streams.pop(key, None)
            self.ttls.pop(key, None)
        return len(keys)


def test_proxy_metrics_summary_counts_stream_and_ttl():
    fake = FakeValkey()
    recorder = ProxyMetricsRecorder(client=fake)
    ctx = ProxyMetricsContext(
        agent_id="456",
        job_run_id="123",
        phase="execution",
        req_model="openai/gpt-4.1-mini",
        model="openai/gpt-4.1-mini",
        provider="openrouter",
    )

    recorder.record_llm_attempt(
        ctx,
        LLMAttemptMetrics(
            upstream_provider="openai",
            status_code=429,
            duration_ms=100,
            retry_num=0,
            error_type="rate_limited",
        ),
    )
    recorder.record_llm_attempt(
        ctx,
        LLMAttemptMetrics(
            upstream_provider="openai",
            status_code=200,
            duration_ms=200,
            input_tokens=10,
            output_tokens=20,
            cached_tokens=3,
            retry_num=1,
        ),
    )
    recorder.record_proxy_complete(ctx, success=True, duration_ms=500)

    summary = recorder.get_summary("123")

    assert "agent_id" not in summary
    assert summary["job_run_id"] == "123"
    assert len(summary["stats"]) == 1

    row = summary["stats"][0]
    assert "agent_id" not in row
    assert row["phase"] == "execution"
    assert row["req_model"] == "openai/gpt-4.1-mini"
    assert row["model"] == "openai/gpt-4.1-mini"
    assert row["provider"] == "openrouter"
    assert row["requests"] == 1
    assert row["success"] == 1
    assert row["error"] == 0
    assert row["llm_requests"] == 2
    assert row["llm_success"] == 1
    assert row["llm_error"] == 1
    assert row["retries"] == 1
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20
    assert row["cached_tokens"] == 3
    assert row["duration_ms_total"] == 500
    assert row["duration_ms_avg"] == 500
    assert row["duration_ms_max"] == 500
    assert row["status_codes"] == {"200": 1, "429": 1}

    stream_keys = [key for key in fake.streams if key.endswith(":llm_stream")]
    assert len(stream_keys) == 1
    assert [entry["retry_num"] for entry in fake.streams[stream_keys[0]]] == ["0", "1"]
    assert all(entry["req_model"] == "openai/gpt-4.1-mini" for entry in fake.streams[stream_keys[0]])
    assert all("agent_id" not in entry for entry in fake.streams[stream_keys[0]])
    assert all("requests" not in entry for entry in fake.streams[stream_keys[0]])
    meta_keys = [key for key in fake.hashes if key.endswith(":meta")]
    assert len(meta_keys) == 1
    assert "agent_id" not in fake.hashes[meta_keys[0]]
    assert all(ttl == METRICS_TTL_SECONDS for ttl in fake.ttls.values())


def test_proxy_metrics_records_request_on_final_response_model_row():
    fake = FakeValkey()
    recorder = ProxyMetricsRecorder(client=fake)
    ctx = ProxyMetricsContext(
        agent_id="456",
        job_run_id="123",
        phase="execution",
        req_model="requested-model",
        model="requested-model",
        provider="openrouter",
    )

    ctx.model = "actual-response-model"
    recorder.record_llm_attempt(ctx, LLMAttemptMetrics(status_code=200))
    recorder.record_proxy_complete(ctx, success=True, duration_ms=500)

    rows = recorder.get_summary("123")["stats"]

    assert len(rows) == 1
    assert rows[0]["req_model"] == "requested-model"
    assert rows[0]["model"] == "actual-response-model"
    assert rows[0]["requests"] == 1
    assert rows[0]["success"] == 1
    assert rows[0]["llm_requests"] == 1


def test_proxy_metrics_reset_deletes_job_run_keys():
    fake = FakeValkey()
    recorder = ProxyMetricsRecorder(client=fake)
    ctx = ProxyMetricsContext(
        agent_id="456",
        job_run_id="123",
        phase="evaluation",
        req_model="model",
        model="model",
        provider="chutes",
    )

    recorder.record_llm_attempt(ctx, LLMAttemptMetrics(status_code=200))
    recorder.reset_summary("123")

    assert fake.hashes == {}
    assert fake.sets == {}
    assert fake.streams == {}


def test_proxy_metrics_groups_same_model_by_provider():
    fake = FakeValkey()
    recorder = ProxyMetricsRecorder(client=fake)
    base = {
        "agent_id": "456",
        "job_run_id": "123",
        "phase": "execution",
        "req_model": "shared-model",
        "model": "shared-model",
    }

    recorder.record_proxy_complete(ProxyMetricsContext(**base, provider="openrouter"), success=True, duration_ms=1)
    recorder.record_proxy_complete(ProxyMetricsContext(**base, provider="chutes"), success=True, duration_ms=1)

    rows = recorder.get_summary("123")["stats"]

    assert sorted(row["provider"] for row in rows) == ["chutes", "openrouter"]
    assert all(row["model"] == "shared-model" for row in rows)


def test_proxy_metrics_missing_valkey_is_noop():
    recorder = ProxyMetricsRecorder(client=None)
    ctx = ProxyMetricsContext(
        agent_id="456",
        job_run_id="123",
        phase="execution",
        req_model="model",
        model="model",
        provider="chutes",
    )

    recorder.record_llm_attempt(ctx, LLMAttemptMetrics(status_code=200))
    recorder.record_proxy_complete(ctx, success=True, duration_ms=1)

    assert recorder.get_summary("123")["stats"] == []
