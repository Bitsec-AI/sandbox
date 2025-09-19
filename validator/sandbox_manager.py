import os
import time
from python_on_whales import docker, Network
from validator.projects import fetch_projects
from python_on_whales.exceptions import DockerException, NoSuchContainer, NoSuchNetwork
from python_on_whales.utils import run

from loggers.logger import get_logger


logger = get_logger()

SANDBOX_IMAGE_TAG = 'bitsec-sandbox:latest'
SANDBOX_CONTAINER_TMPL = 'bitsec_sandbox_{job_id}_{project_id}'
PROXY_NETWORK = 'bitsec-net'
PROXY_IMAGE_TAG = 'bitsec-proxy:latest'
PROXY_CONTAINER = 'bitsec_proxy'
PROXY_PORT = os.getenv("PROXY_PORT", 8087)


class SandboxManager:
    def __init__(self):
        fetch_projects()
        self.curr_dir = os.path.dirname(os.path.abspath(__file__))
        self.proxy_dir = os.path.join(self.curr_dir, 'proxy')
        self.projects_dir = os.path.join(self.curr_dir, 'projects')
        self.all_jobs_dir = os.path.join(self.curr_dir, 'jobs')

        self.build_images()
        self.init_proxy()

    def run(self):
        jobs = [{
            "job_id": 1,
            "agent_file": "agent.py",
        }]

        while True:
            if not jobs:
                time.sleep(60)
                continue

            job = jobs.pop(0)
            self.process_job(job['job_id'], job.get('agent_file'))

    def build_images(self):
        docker.build(self.curr_dir, tags=SANDBOX_IMAGE_TAG)
        docker.build(self.proxy_dir, tags="bitsec-proxy:latest")

    def create_internal_network(self, name):
        full_cmd = docker.network.docker_cmd + ["network", "create"]
        full_cmd.add_flag("--internal", True)
        full_cmd.append(name)
        return Network(docker.network.client_config, run(full_cmd), is_immutable_id=True)

    def init_proxy(self):
        docker.remove(PROXY_CONTAINER, force=True)

        try:
            docker.network.inspect(PROXY_NETWORK)
        except NoSuchNetwork:
            self.create_internal_network(PROXY_NETWORK)

        docker.run(
            PROXY_IMAGE_TAG,
            name=PROXY_CONTAINER,
            detach=True,
            publish=[(PROXY_PORT, 8000)],
            envs={
                "CHUTES_API_KEY": os.getenv("CHUTES_API_KEY"),
            },
        )
        docker.network.connect(PROXY_NETWORK, PROXY_CONTAINER)

    def remove_container(self, container_name):
        try:
            docker.remove(container_name, force=True)

        except DockerException as e:
            logger.error(f"Exit code {e.return_code} while running {e.docker_command}")
            raise

    def process_job(self, job_id, agent_filepath='agent.py'):
        logger.info(f"Processing job ID: {job_id}")
        job_dir = os.path.join(self.all_jobs_dir, f"job_{job_id}")
        reports_dir = os.path.join(job_dir, "reports")
        os.makedirs(reports_dir, exist_ok=True)

        if not agent_filepath:
            # TODO: download agent file
            pass

        agent_filepath = os.path.abspath(agent_filepath)

        project_ids = [
            name for name in os.listdir(self.projects_dir)
            if os.path.isdir(os.path.join(self.projects_dir, name))
        ]
        logger.info(f"Found {len(project_ids)} projects")

        for project_id in project_ids:
            self.process_job_project(job_id, project_id, agent_filepath, reports_dir)

    def process_job_project(self, job_id, project_id, agent_filepath, reports_dir):
        project_report_dir = os.path.join(reports_dir, f"{project_id}")
        os.makedirs(project_report_dir, exist_ok=True)

        project_code_dir = os.path.join(self.projects_dir, f"{project_id}")

        sandbox_container = SANDBOX_CONTAINER_TMPL.format(
            job_id=job_id,
            project_id=project_id,
        )

        run_id = f"[J:{job_id}|P:{project_id}]"

        # clear any previous container runs
        self.remove_container(sandbox_container)

        logger.info(f"{run_id} Starting container")
        container = docker.run(
            SANDBOX_IMAGE_TAG,
            name=sandbox_container,
            networks=[PROXY_NETWORK],
            volumes=[
                (agent_filepath, '/app/agent.py'),
                (project_code_dir, '/app/project_code'),
            ],
            detach=True,
        )
        docker.wait(container)

        try:
            docker.copy((container, "/app/report.json"), project_report_dir)
            logger.info(f"{run_id} Finished processing. Report copied: {project_id}")

        except DockerException as e:
            if e.return_code == 1 and "does not exist" in str(e):
                logger.error(f"{run_id} Report not found in container")
            else:
                raise

        container.remove()

if __name__ == '__main__':
    m = SandboxManager()
    # m.run()
    m.process_job('local', agent_filepath="miner/agent.py")
