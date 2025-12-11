#!/usr/bin/env python3
"""
bitsec.py

Bitsec CLI utility for miners and validators.
Operations are signed with a Bittensor wallet hotkey.
"""

import os
import subprocess
import sys
from pathlib import Path

import typer
from typer import Option, Argument

from config import settings
from loggers.logger import get_logger
from validator.platform_client import PlatformClient
from validator.models.platform import User, UserRole, AgentCode
from validator.manager import SandboxManager

logger = get_logger()

# -------------------------------------------------------
# Typer apps
# -------------------------------------------------------
app = typer.Typer(help="Bitsec CLI for miners and validators", pretty_exceptions_enable=False)
miner_app = typer.Typer(help="Miner operations")
validator_app = typer.Typer(help="Validator operations")

# Register sub-apps
app.add_typer(miner_app, name="miner")
app.add_typer(validator_app, name="validator")

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------
def get_platform_client(wallet: str | None = None) -> PlatformClient:
    wallet_name = wallet or settings.wallet_name
    return PlatformClient(settings.platform_url, wallet_name=wallet_name)

# -------------------------------------------------------
# Original helper functions (unchanged)
# -------------------------------------------------------
def create_user(
    email: str,
    name: str | None,
    client: PlatformClient,
    is_miner: bool = True,
) -> None:
    """Register a miner or validator (depending on role)."""
    user = User(
        email=email,
        name=name,
        role=UserRole.MINER if is_miner else UserRole.VALIDATOR,
    )
    user = client.create_user(user)

    logger.info(f"{user['role']} User {user['email']} created with hotkey: {user['hotkey']}")

# -------------------------------------------------------
# Miner commands
# -------------------------------------------------------
@miner_app.command("create")
def miner_create(
    email: str = Argument(..., help="Email of the miner"),
    name: str | None = Argument(None, help="Optional name"),
    wallet: str | None = Option(None, help="Bittensor wallet name"),
):
    """Create a miner user on the platform (registers with hotkey)."""
    client = get_platform_client(wallet)
    create_user(email=email, name=name, client=client, is_miner=True)

@miner_app.command("submit")
def miner_submit(wallet: str | None = Option(None, help="Bittensor wallet name")):
    """Submit the miner agent code to the platform."""
    agent_path = Path("miner/agent.py")
    if not agent_path.exists():
        raise FileNotFoundError(agent_path)

    code_str = agent_path.read_text(encoding="utf-8")
    agent_code = AgentCode(code=code_str)

    client = get_platform_client(wallet)
    agent = client.submit_agent(agent_code)
    logger.info(f"Agent submitted: version {agent['version']}")

@miner_app.command("run")
def miner_run():
    """Run the agent execution and evaluation locally via Docker (recommended)."""
    env = os.environ.copy()
    env["LOCAL"] = "true"

    cmd = ["docker", "compose", "up", "--build"]
    subprocess.run(cmd, env=env, check=True)

@miner_app.command("run-no-docker")
def miner_run_no_docker():
    """Run the agent execution and evaluation locally as a script"""
    os.environ["LOCAL"] = "true"
    manager = SandboxManager(is_local=True)
    manager.run()

@miner_app.command("execute-agent")
def miner_execute_agent():
    """Run the miner agent script locally on a single project."""
    cmd = [sys.executable, "miner/agent.py"]
    subprocess.run(cmd, env=os.environ.copy(), check=True)

# -------------------------------------------------------
# Validator commands
# -------------------------------------------------------
@validator_app.command("create")
def validator_create(
    email: str = Argument(..., help="Email of the validator"),
    name: str | None = Argument(None, help="Optional name"),
    wallet: str | None = Option(None, help="Bittensor wallet name"),
):
    """Create a validator account."""
    client = get_platform_client(wallet)
    create_user(email=email, name=name, client=client, is_miner=False)

@validator_app.command("run")
def validator_run():
    """Run the validator."""
    cmd = [
        "docker", "compose",
        "-f", "docker-compose.validator.yaml",
        "up", "--build", "-d",
    ]
    subprocess.run(cmd, check=True)

# -------------------------------------------------------
# Entry point
# -------------------------------------------------------
if __name__ == "__main__":
    app()
