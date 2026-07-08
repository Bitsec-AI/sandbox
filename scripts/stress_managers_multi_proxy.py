#!/usr/bin/env python3
"""Bootstrap the proxy, then run four named validator containers."""

import os
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from validator.manager import SandboxManager  # noqa: E402


DEFAULT_COUNT = 4
DEFAULT_START_DELAY_SECONDS = 2
DEFAULT_BASE_PROXY_PORT = 8087
DEFAULT_PROXY_WAIT_SECONDS = 10
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
COMPOSE_HOST_FILE = PROJECT_ROOT / "scripts" / "compose.host.yml"


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        parsed = int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc

    if parsed < 1:
        raise SystemExit(f"{name} must be >= 1, got {parsed}")

    return parsed


def run_bootstrap_manager() -> None:
    print("[bootstrap] initializing default SandboxManager")
    SandboxManager(is_local=True)
    print("[bootstrap] default SandboxManager initialized")


def cleanup_existing_stress_containers(count: int) -> None:
    container_names = []
    for index in range(1, count + 1):
        container_names.append(f"bitsec_validator_stress_{index}")
        container_names.append(f"bitsec_proxy_stress_{index}")

    print("[stress] removing existing stress containers")
    subprocess.run(["docker", "rm", "-f", *container_names], check=False)


def start_stress_validator(index: int, proxy_port: int) -> None:
    validator_name = f"bitsec_validator_stress_{index}"
    proxy_name = f"bitsec_proxy_stress_{index}"
    proxy_url = f"http://host.docker.internal:{proxy_port}"
    env_overrides = {
        "LOCAL": "true",
        "PROXY_CONTAINER": proxy_name,
        "PROXY_PORT": str(proxy_port),
        "PROXY_URL": proxy_url,
    }

    cmd = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(COMPOSE_HOST_FILE),
        "run",
        "--build",
        "-d",
        "--name",
        validator_name,
    ]
    for key, value in env_overrides.items():
        cmd.extend(["-e", f"{key}={value}"])
    cmd.append("validator")

    print(f"[stress-{index}] starting {validator_name} with {proxy_name} on host port {proxy_port}")
    subprocess.run(cmd, check=True)


def list_stress_proxy_containers() -> set[str]:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "name=bitsec_proxy_stress_",
            "--format",
            "{{.Names}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def wait_for_stress_proxies(count: int, timeout_seconds: int) -> None:
    expected = {f"bitsec_proxy_stress_{index}" for index in range(1, count + 1)}
    deadline = time.monotonic() + timeout_seconds

    while True:
        running = list_stress_proxy_containers()
        missing = sorted(expected - running)
        if not missing:
            print("[stress] all stress proxy containers are running")
            return

        if time.monotonic() >= deadline:
            print(f"[stress] timed out waiting for proxy containers: {', '.join(missing)}")
            return

        print(f"[stress] waiting for proxy containers: {', '.join(missing)}")
        time.sleep(5)


def print_matching_containers() -> None:
    print("[stress] matching validator containers")
    subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "name=bitsec_validator_stress_",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
        ],
        check=False,
    )

    print("[stress] matching proxy containers")
    subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "name=bitsec_proxy_stress_",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
        ],
        check=False,
    )


def main() -> int:
    count = env_int("COUNT", DEFAULT_COUNT)
    start_delay_seconds = env_int("START_DELAY_SECONDS", DEFAULT_START_DELAY_SECONDS)
    base_proxy_port = env_int("BASE_PROXY_PORT", DEFAULT_BASE_PROXY_PORT)
    proxy_wait_seconds = env_int("PROXY_WAIT_SECONDS", DEFAULT_PROXY_WAIT_SECONDS)

    cleanup_existing_stress_containers(count)
    run_bootstrap_manager()

    for index in range(1, count + 1):
        start_stress_validator(index, base_proxy_port + index)
        if index != count:
            time.sleep(start_delay_seconds)

    wait_for_stress_proxies(count, proxy_wait_seconds)
    print_matching_containers()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
