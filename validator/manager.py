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
from validator.proxy_client import ProxyClient, ProxyClientError
from validator.executor import AgentExecutor
from validator.evaluator import AgentEvaluator


logger = get_logger()

PROXY_IMAGE_TAG = "bitsec-proxy:latest"


class SandboxManager:
    def __init__(self, is_local=False, wallet_name=None):
        self.proxy_docker_dir = os.path.join(settings.validator_dir, "proxy")
        self.all_jobs_dir = os.path.join(os.getcwd(), "jobs")
        self.host_jobs_dir = os.path.join(settings.host_cwd, "jobs")
        self.platform_client = PlatformClient(is_local=is_local, wallet_name=wallet_name)
        self.proxy_client = ProxyClient()
        self.validator = self.platform_client.get_current_validator()
        self.execution_pool = ThreadPoolExecutor(max_workers=settings.execution_workers)
        self.scoring_pool = ThreadPoolExecutor(max_workers=settings.scoring_workers)
        self.execution_queue_task = None
        self.scoring_queue_task = None

        self.validator_id = self.validator["id"]

        self.build_images()
        self.init_proxy()

        self.is_local = is_local

    async def run(self):
        if self.is_local:
            await self.poll_execution_job_run()
            await self.poll_scoring_job_run()
            return

        while True:
            await self.run_once()
            await asyncio.sleep(60)

    async def poll_job_run(self):
        return await self.poll_execution_job_run()

    async def run_once(self):
        self.raise_queue_errors()
        queue_started = self.ensure_queue_tasks()
        await asyncio.sleep(0)
        self.raise_queue_errors()
        return queue_started

    async def run_execution_queue(self):
        while True:
            has_job = await self.poll_execution_job_run()
            if not has_job:
                await asyncio.sleep(60)

    async def run_scoring_queue(self):
        while True:
            has_job = await self.poll_scoring_job_run()
            if not has_job:
                await asyncio.sleep(60)

    def queue_tasks(self):
        return {
            "execution": self.execution_queue_task,
            "scoring": self.scoring_queue_task,
        }

    def raise_queue_errors(self):
        for queue_name, task in self.queue_tasks().items():
            if task is None or not task.done():
                continue

            if task.cancelled():
                raise RuntimeError(f"{queue_name} queue task was cancelled")

            exception = task.exception()
            if exception:
                raise RuntimeError(f"{queue_name} queue crashed") from exception

            raise RuntimeError(f"{queue_name} queue stopped unexpectedly")

    def ensure_queue_tasks(self):
        if settings.skip_execution and settings.skip_evaluation:
            logger.warning("Both execution and evaluation are disabled. Nothing to poll.")
            return False

        started = False
        if not settings.skip_execution and self.execution_queue_task is None:
            self.execution_queue_task = asyncio.create_task(
                self.run_execution_queue(),
                name="execution-queue",
            )
            started = True

        if not settings.skip_evaluation and self.scoring_queue_task is None:
            self.scoring_queue_task = asyncio.create_task(
                self.run_scoring_queue(),
                name="scoring-queue",
            )
            started = True

        return started

    async def _send_heartbeat(self):
        if not self.is_local:
            try:
                self.platform_client.send_heartbeat()
            except Exception as e:
                logger.error(f"Failed to send heartbeat: {e}")

    async def poll_execution_job_run(self):
        await self._send_heartbeat()

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

        await self.process_execution_job_run(job_run)
        return True

    async def poll_scoring_job_run(self):
        await self._send_heartbeat()

        retries = 10
        job_run = None
        for _ in range(retries):
            try:
                job_run = self.platform_client.get_next_scoring_job_run(self.validator_id)
                break
            except PlatformError as e:
                logger.error(f"Error fetching scoring job run: {e}")
                await asyncio.sleep(1)

        if not job_run:
            logger.info("No scoring job runs available")
            return False

        await self.process_scoring_job_run(job_run)
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
        )
        docker.network.connect(settings.proxy_network, settings.proxy_container)

        # wait for proxy to start up
        time.sleep(10)

    def reset_proxy_summary(self, job_run_id: int, agent_id: int):
        try:
            self.proxy_client.reset_job_run_summary(job_run_id)
        except Exception as e:
            logger.warning(f"[A:{agent_id}|JR:{job_run_id}] Failed to reset proxy summary: {e}")

    def fetch_proxy_summary(self, job_run_id: int, agent_id: int) -> dict | None:
        try:
            return self.proxy_client.get_job_run_summary(job_run_id)
        except Exception as e:
            logger.warning(f"[A:{agent_id}|JR:{job_run_id}] Failed to fetch proxy summary: {e}")
            return None

    def submit_proxy_summary(self, job_run_id: int, agent_id: int):
        summary = self.fetch_proxy_summary(job_run_id, agent_id)
        if not summary:
            return
        try:
            self.platform_client.submit_job_run_proxy_summary(job_run_id, summary)
        except Exception as e:
            logger.warning(f"[A:{agent_id}|JR:{job_run_id}] Failed to submit proxy summary: {e}")

    async def process_job_run(self, job_run):
        await self.process_execution_job_run(job_run)
        if not settings.skip_evaluation:
            await self.process_scoring_job_run(job_run)

    async def process_execution_job_run(self, job_run):
        logger.info(f"[A:{job_run.agent_id}|JR:{job_run.id}] Processing execution job run")

        self.platform_client.start_job_run(job_run.id)
        self.reset_proxy_summary(job_run.id, job_run.agent_id)

        job_run_dir = os.path.join(self.all_jobs_dir, f"job_run_{job_run.id}")
        job_run_reports_dir = os.path.join(job_run_dir, "reports")
        os.makedirs(job_run_reports_dir, exist_ok=True)

        agent = self.platform_client.get_job_run_agent(job_run_id=job_run.id)

        execution_api_key = agent.get("execution_api_key")
        if not execution_api_key:
            logger.error(
                f"[A:{job_run.agent_id}|JR:{job_run.id}] Miner did not provide an execution API key. Skipping job run."
            )
            self.platform_client.complete_job_run(job_run.id, status="error")
            return

        try:
            self.proxy_client.validate_auth(execution_api_key)
        except ProxyClientError:
            logger.error(
                f"[A:{job_run.agent_id}|JR:{job_run.id}] Miner inference authentication failed. Skipping job run."
            )
            self.platform_client.complete_job_run(job_run.id, status="error")
            return

        eval_max_vulns = agent.get("eval_max_vulns", 100)
        logger.info(f"[A:{job_run.agent_id}|JR:{job_run.id}] Eval max vulnerabilities: {eval_max_vulns}")

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
        executors = []

        for project_key in agent["project_keys"]:
            executor = AgentExecutor(
                job_run,
                agent_filepath,
                project_key,
                job_run_reports_dir,
                platform_client=self.platform_client,
                execution_api_key=execution_api_key,
                eval_max_vulns=eval_max_vulns,
            )
            executors.append(executor)
            task = loop.run_in_executor(self.execution_pool, executor.run_execution)
            tasks.append(task)

        await asyncio.gather(*tasks)

        for executor in executors:
            executor.agent_execution_id = executor.submit_agent_execution()

        self.platform_client.start_job_run_evaluation(job_run.id)

    async def process_scoring_job_run(self, job_run):
        logger.info(f"[A:{job_run.agent_id}|JR:{job_run.id}] Processing scoring job run")

        agent = self.platform_client.get_job_run_agent(job_run_id=job_run.id)
        eval_max_vulns = agent.get("eval_max_vulns", 100)
        executions = self.platform_client.get_job_run_executions(job_run.id)

        if not executions:
            logger.error(f"[A:{job_run.agent_id}|JR:{job_run.id}] No submitted executions available for scoring")
            self.platform_client.complete_job_run(job_run.id, status="error")
            self.submit_proxy_summary(job_run.id, job_run.agent_id)
            return

        loop = asyncio.get_running_loop()
        tasks = []

        for execution in executions:
            evaluator = AgentEvaluator(
                job_run=job_run,
                platform_client=self.platform_client,
                agent_execution_id=execution.id,
                project_key=execution.project,
                eval_max_vulns=execution.eval_max_vulns or eval_max_vulns,
            )
            task = loop.run_in_executor(self.scoring_pool, evaluator.eval_agent_execution, execution)
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        status = "error" if any(result.get("status") == "error" for result in results if result) else "success"
        self.platform_client.complete_job_run(job_run.id, status=status)
        self.submit_proxy_summary(job_run.id, job_run.agent_id)


if __name__ == "__main__":
    LOCAL = settings.local
    logger.info(f"LOCAL: {LOCAL}")
    m = SandboxManager(is_local=LOCAL)
    asyncio.run(m.run())
