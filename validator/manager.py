import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor

from python_on_whales import docker, Network
from python_on_whales.exceptions import NoSuchNetwork
from python_on_whales.utils import run

from config import settings
from loggers.logger import get_logger
from validator.platform_client import PlatformClient, PlatformError
from validator.executor import AgentExecutor


logger = get_logger()

PROXY_IMAGE_TAG = "bitsec-proxy:latest"


class SandboxManager:
    def __init__(self, is_local=False, wallet_name=None):
        self.proxy_docker_dir = os.path.join(settings.validator_dir, "proxy")
        self.all_jobs_dir = os.path.join(os.getcwd(), "jobs")
        self.host_jobs_dir = os.path.join(settings.host_cwd, "jobs")
        self.platform_client = PlatformClient(is_local=is_local, wallet_name=wallet_name)
        self.validator = self.platform_client.get_current_validator()
        self.executor_pool = ThreadPoolExecutor(max_workers=12)

        self.validator_id = self.validator["id"]

        self.build_images()
        self.init_proxy()

        self.is_local = is_local

    async def run(self):
        while True:
            has_job = await self.poll_job_run()
            if not has_job:
                await asyncio.sleep(60)

            if self.is_local:
                break

    async def poll_job_run(self):
        if not self.is_local:
            try:
                self.platform_client.send_heartbeat()
            except Exception as e:
                logger.error(f"Failed to send heartbeat: {e}")

        retries = 10
        job_run = None
        for _ in range(retries):
            try:
                job_run = self.platform_client.get_next_job_run(self.validator_id)
                break
            except PlatformError as e:
                logger.error(f"Error fetching job run: {e}")
                await asyncio.sleep(1)

        if not job_run:
            logger.info("No job runs available")
            return False

        await self.process_job_run(job_run)
        return True

    def build_images(self):
        docker.build(
            self.proxy_docker_dir,
            tags="bitsec-proxy:latest",
            build_contexts={"loggers": "loggers"},
        )

    def create_internal_network(self, name):
        full_cmd = docker.network.docker_cmd + ["network", "create"]
        full_cmd.add_flag("--internal", True)
        full_cmd.append(name)
        return Network(docker.network.client_config, run(full_cmd), is_immutable_id=True)

    def init_proxy(self):
        docker.remove(settings.proxy_container, force=True)

        try:
            docker.network.inspect(settings.proxy_network)
        except NoSuchNetwork:
            self.create_internal_network(settings.proxy_network)

        docker.run(
            PROXY_IMAGE_TAG,
            name=settings.proxy_container,
            detach=True,
            publish=[(settings.proxy_port, 8000)],
            envs={
                "CHUTES_API_KEY": settings.chutes_api_key,
            },
        )
        docker.network.connect(settings.proxy_network, settings.proxy_container)

        # wait for proxy to start up
        time.sleep(10)

    async def process_job_run(self, job_run):
        logger.info(f"[J:{job_run.job_id}|JR:{job_run.id}] Processing job run")

        self.platform_client.start_job_run(job_run.id)

        job_run_dir = os.path.join(self.all_jobs_dir, f"job_run_{job_run.id}")
        job_run_reports_dir = os.path.join(job_run_dir, "reports")
        os.makedirs(job_run_reports_dir, exist_ok=True)

        agent = self.platform_client.get_job_run_agent(job_run_id=job_run.id)

        if self.is_local:
            agent_filepath = f"{settings.host_cwd}/miner/agent.py"
            agent_filepath = os.path.abspath(agent_filepath)

        else:
            agent_filepath_rel = os.path.join(job_run_dir, "agent.py")
            with open(agent_filepath_rel, "w", encoding="utf-8") as f:
                f.write(agent["code"])

            agent_filepath = os.path.join(
                self.host_jobs_dir,
                f"job_run_{job_run.id}",
                "agent.py",
            )

        loop = asyncio.get_running_loop()
        tasks = []

        for project_key in agent["project_keys"]:
            executor = AgentExecutor(
                job_run,
                agent_filepath,
                project_key,
                job_run_reports_dir,
                platform_client=self.platform_client,
            )
            task = loop.run_in_executor(self.executor_pool, executor.run)
            tasks.append(task)

        await asyncio.gather(*tasks)

        # TODO: Check if finished successfully or part-fail
        self.platform_client.complete_job_run(job_run.id)


if __name__ == "__main__":
    LOCAL = settings.local
    logger.info(f"LOCAL: {LOCAL}")
    m = SandboxManager(is_local=LOCAL)
    asyncio.run(m.run())
