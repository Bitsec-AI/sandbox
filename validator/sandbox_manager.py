import json
import logging
import os
import requests
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from python_on_whales import docker, Network
from python_on_whales.exceptions import DockerException, NoSuchContainer, NoSuchNetwork
from python_on_whales.utils import run

from config import settings
from loggers.logger import get_logger
from validator.models.platform import JobRun, AgentExecution, AgentEvaluation
from validator.platform_client import PlatformClient
from validator.scorer import ScaBenchScorerV2


load_dotenv()
logger = get_logger()

PROJECT_IMAGE_TAG_TMPL = 'ghcr.io/bitsec-ai/{project_key}:latest'
SANDBOX_CONTAINER_TMPL = 'bitsec_sandbox_{job_run_id}_{project_key}'
PROXY_NETWORK = 'bitsec-net'
PROXY_IMAGE_TAG = 'bitsec-proxy:latest'
PROXY_CONTAINER = 'bitsec_proxy'

HOST_CWD = settings.host_cwd
VALIDATOR_DIR = Path('validator')
HOST_PROJECTS_DIR = os.path.abspath(os.path.join(HOST_CWD, VALIDATOR_DIR, 'projects'))



class SandboxManager:
    def __init__(self, is_local=False):
        self.proxy_docker_dir = os.path.join(VALIDATOR_DIR, 'proxy')
        self.validator_docker_dir = VALIDATOR_DIR

        self.projects_dir = os.path.join(VALIDATOR_DIR, 'projects')
        self.all_jobs_dir = os.path.join(VALIDATOR_DIR, 'jobs')

        self.platform_client = PlatformClient(is_local=is_local)
        self.validator = self.platform_client.get_current_validator()

        self.validator_id = self.validator['id']

        self.build_images()
        self.init_proxy()

        self.is_local = is_local

    def run(self):
        while True:
            job_run = self.platform_client.get_next_job_run(self.validator_id)
            if job_run:
                self.process_job_run(job_run)

            else:
                logger.info("No job runs available")
                time.sleep(60)

            if self.is_local:
                break

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
        docker.remove(PROXY_CONTAINER, force=True)

        try:
            docker.network.inspect(PROXY_NETWORK)
        except NoSuchNetwork:
            self.create_internal_network(PROXY_NETWORK)

        docker.run(
            PROXY_IMAGE_TAG,
            name=PROXY_CONTAINER,
            detach=True,
            publish=[(settings.proxy_port, 8000)],
            envs={
                "CHUTES_API_KEY": os.getenv("CHUTES_API_KEY"),
            },
        )
        docker.network.connect(PROXY_NETWORK, PROXY_CONTAINER)

    def get_project_keys(self):
        projects = self.platform_client.get_projects()
        project_keys = [p['project_key'] for p in projects]
        return project_keys

    def process_job_run(self, job_run, skip_run=False):
        logger.info(f"[J:{job_run.job_id}|JR:{job_run.id}] Processing job run")

        self.platform_client.start_job_run(job_run.id)

        job_run_dir = os.path.join(self.all_jobs_dir, f"job_run_{job_run.id}")
        job_run_reports_dir = os.path.join(job_run_dir, "reports")
        os.makedirs(job_run_reports_dir, exist_ok=True)

        agent = self.platform_client.get_job_run_code(job_run_id=job_run.id)

        if self.is_local:
            agent_filepath = f"{HOST_CWD}/miner/agent.py"
            agent_filepath = os.path.abspath(agent_filepath)

        else:
            agent_filepath_rel = os.path.join(job_run_dir, 'agent.py')
            with open(agent_filepath_rel, "w", encoding="utf-8") as f:
                f.write(agent['code'])

            agent_filepath = os.path.join(HOST_CWD, agent_filepath_rel)

        for project_key in agent['project_keys']:
            executor = AgentExecutor(
                job_run,
                agent_filepath,
                project_key,
                job_run_reports_dir,
                platform_client=self.platform_client,
            )
            executor.run()

        # TODO: Check if finished successfully or part-fail
        self.platform_client.complete_job_run(job_run.id)


class AgentExecutor:
    def __init__(
        self,
        job_run,
        agent_filepath,
        project_key,
        job_run_reports_dir,
        platform_client,
    ):
        self.job_run = job_run
        self.agent_filepath = agent_filepath
        self.project_key = project_key
        self.job_run_reports_dir = job_run_reports_dir
        self.platform_client = platform_client

        self.project_report_dir = os.path.join(self.job_run_reports_dir, f"{self.project_key}")
        os.makedirs(self.project_report_dir, exist_ok=True)

        self.agent_execution_id: int | None = None
        self.agent_evaluation_id: int | None = None

        self.init_logger()

    def init_logger(self):
        log_prefix = f"[J:{self.job_run.job_id}|JR:{self.job_run.id}|P:{self.project_key}]"
        self.logger = logging.LoggerAdapter(logger, {'prefix': log_prefix})

        def process(msg, kwargs):
            return f"{log_prefix} {msg}", kwargs
        
        self.logger.process = process

    def remove_container(self, container_name):
        try:
            docker.remove(container_name, force=True)

        except DockerException as e:
            logger.error(f"Exit code {e.return_code} while running {e.docker_command}")
            raise

    def run(self):
        self.started_at = datetime.utcnow()

        if not settings.skip_execution:
            self.run_project()
            self.agent_execution_id = self.submit_agent_execution()

        if not settings.skip_evaluation:
            self.eval_job_runs()

    def run_project(self):
        sandbox_container = SANDBOX_CONTAINER_TMPL.format(
            job_run_id=self.job_run.id,
            project_key=self.project_key,
        )

        # clear any previous container runs
        self.remove_container(sandbox_container)

        project_image_tag = PROJECT_IMAGE_TAG_TMPL.format(project_key=self.project_key)

        self.logger.info(f"Starting container")
        container = docker.run(
            project_image_tag,
            name=sandbox_container,
            networks=[PROXY_NETWORK],
            volumes=[
                (self.agent_filepath, '/app/agent.py'),
            ],
            envs={
                "JOB_RUN_ID": self.job_run.id,
                "PROJECT_KEY": self.project_key,
            },
            detach=True,
        )
        docker.wait(container)

        try:
            docker.copy((container, "/app/report.json"), self.project_report_dir)
            self.logger.info(f"Finished processing. Report copied: {self.project_key} {self.project_report_dir}")

        except DockerException as e:
            if e.return_code == 1 and "does not exist" in str(e):
                logger.error(f"Report not found in container")
            else:
                raise

        container.remove()

    def submit_agent_execution(self):
        report_filepath = os.path.join(self.project_report_dir, 'report.json')
        if not Path(report_filepath).is_file():
            self.logger.error(f"Report not found")
            return # TODO: submit with error

        with open(report_filepath, "r", encoding="utf-8") as f:
            report_dict = json.load(f)

        report_dict['validator_id'] = self.job_run.validator_id
        report_dict['job_run_id'] = self.job_run.id
        report_dict['project'] = self.project_key
        report_dict['status'] = 'success'
        report_dict['started_at'] = self.started_at
        report_dict['completed_at'] = datetime.utcnow()

        agent_execution = AgentExecution.model_validate(report_dict)

        try:
            resp = self.platform_client.submit_agent_execution(agent_execution)
            return resp['id']

        except Exception as e:
            self.logger.exception(f"Error submitting agent execution: {e}")
            return

    def submit_agent_evaluation(self, project_scoring_results):
        if not self.agent_execution_id:
            self.logger.info("Not running from agent execution. Skipping submit evaluation")
            return

        scoring_data = {}
        scoring_data['agent_execution_id'] = self.agent_execution_id
        scoring_data['status'] = project_scoring_results['status']
        scoring_data.update(project_scoring_results['result'])

        agent_evaluation = AgentEvaluation.model_validate(scoring_data)

        try:
            resp = self.platform_client.submit_agent_evaluation(agent_evaluation)
            agent_evaluation_id = resp['id']
            return agent_evaluation_id

        except Exception as e:
            self.logger.exception(f"Error submitting agent execution: {e}")
            return

    def eval_job_runs(self):
        """
        Evaluate all reports in the reports directory using ScaBenchScorerV2.
        
        Args:
            reports_dir (str): Path to the directory containing report JSON files
            project_key (str, optional): Specific project ID to evaluate. If None, evaluates all projects.
            
        Returns:
            dict: Summary of scoring results for all projects
        """
        logger.info(f"Starting evaluation of reports in: {self.job_run_reports_dir}")
        if self.project_key:
            logger.info(f"Filtering to specific project: {self.project_key}")

        # Load benchmark data
        benchmark_file = os.path.join(VALIDATOR_DIR, 'curated-highs-only-2025-08-08.json')
        if not os.path.exists(benchmark_file):
            logger.error(f"Benchmark file not found: {benchmark_file}")
            return {}

        with open(benchmark_file, 'r') as f:
            benchmark_data = json.load(f)

        # Create a mapping of project_key to expected vulnerabilities
        benchmark_map = {}
        for entry in benchmark_data:
            entry_project_id = entry.get('project_id')
            vulnerabilities = entry.get('vulnerabilities', [])
            if entry_project_id and vulnerabilities:
                benchmark_map[entry_project_id] = vulnerabilities

        logger.info(f"Loaded benchmark data for {len(benchmark_map)} projects")

        # Initialize the scorer
        scorer_config = {
            'model': 'gpt-4o',
            'debug': True,
            'verbose': True,
            'confidence_threshold': 0.75,
            'strict_matching': False
        }
        scorer = ScaBenchScorerV2(scorer_config)

        reports_path = Path(self.job_run_reports_dir)

        # Look for report.json files in subdirectories
        if self.project_key:
            # Look for specific project report
            specific_report = reports_path / self.project_key / "report.json"
            if specific_report.exists():
                report_files = [specific_report]
            else:
                logger.error(f"Report not found for project {project_key}")
                return {}
        else:
            # Look for all report.json files
            report_files = list(reports_path.glob("*/report.json"))
        
        if not report_files:
            logger.warning(f"No report.json files found in {reports_path}")
            return {}

        logger.info(f"Found {len(report_files)} report files to evaluate")

        scoring_results = {}

        for report_file in report_files:
            current_project_key = report_file.parent.name
            logger.info(f"Evaluating project: {current_project_key}")

            try:
                # Load the report
                with open(report_file, 'r') as f:
                    report_data = json.load(f)

                # Check if the report contains successful findings
                if not report_data.get('success', False):
                    logger.warning(f"Report for {current_project_key} indicates failure: {report_data.get('error', 'Unknown error')}")
                    # Create a mock result for failed reports
                    scoring_results[current_project_key] = {
                        'status': 'failed',
                        'error': report_data.get('error', 'Unknown error'),
                        'stdout': report_data.get('stdout', ''),
                        'stderr': report_data.get('stderr', '')
                    }
                    # Persist per-project scoring summary with found=0 on failure
                    try:
                        expected_findings = benchmark_map.get(current_project_key, [])
                        project_summary = {
                            'project': current_project_key,
                            'timestamp': report_data.get('report', {}).get('timestamp') or '',
                            'expected': len(expected_findings),
                            'found': 0
                        }
                        summary_path = report_file.parent / "scoring_summary.json"
                        with open(summary_path, 'w') as sf:
                            json.dump(project_summary, sf, indent=2)
                    except Exception as e:
                        logger.error(f"Failed to write failure scoring summary for {current_project_key}: {str(e)}")
                    continue

                # Extract findings from the report
                tool_findings = report_data.get('findings', [])

                # If no findings at top level, check under 'report.vulnerabilities'
                if not tool_findings and 'report' in report_data:
                    tool_findings = report_data['report'].get('vulnerabilities', [])

                if not tool_findings:
                    logger.warning(f"No findings found in report for {current_project_key}")
                    scoring_results[current_project_key] = {
                        'status': 'no_findings',
                        'message': 'No findings reported by the tool'
                    }
                    continue

                # Get expected vulnerabilities from benchmark data
                expected_findings = benchmark_map.get(current_project_key, [])

                if not expected_findings:
                    logger.warning(f"No benchmark data found for project {current_project_key}")
                    scoring_results[current_project_key] = {
                        'status': 'no_benchmark',
                        'message': f'No benchmark data available for project {current_project_key}',
                        'tool_findings_count': len(tool_findings)
                    }
                    continue

                logger.info(f"Scoring {current_project_key} with {len(expected_findings)} expected vulnerabilities and {len(tool_findings)} tool findings")

                # Score the project
                result = scorer.score_project(
                    expected_findings=expected_findings,
                    tool_findings=tool_findings,
                    project_name=current_project_key,
                )

                project_scoring_results = {
                    'status': 'success',
                    'result': {
                        'project': result.project,
                        'timestamp': result.timestamp,
                        'total_expected': result.total_expected,
                        'total_found': result.total_found,
                        'true_positives': result.true_positives,
                        'false_negatives': result.false_negatives,
                        'false_positives': result.false_positives,
                        'detection_rate': result.detection_rate,
                        'precision': result.precision,
                        'f1_score': result.f1_score,
                        'matched_findings': result.matched_findings,
                        'missed_findings': result.missed_findings,
                        'extra_findings': result.extra_findings,
                        'undecided_findings': result.undecided_findings,
                    }
                }

                # Store the scoring result
                scoring_results[current_project_key] = project_scoring_results

                self.agent_evaluation_id = self.submit_agent_evaluation(project_scoring_results=project_scoring_results)

                # Concise summary for CI/logs: true positives vs expected
                logger.info(
                    f"Scoring Project: {current_project_key} | Found: {result.true_positives} | Expected: {result.total_expected}"
                )

                # Persist per-project scoring summary next to report.json
                try:
                    project_summary = {
                        'project': current_project_key,
                        'timestamp': result.timestamp,
                        'expected': result.total_expected,
                        'found': result.true_positives
                    }
                    summary_path = report_file.parent / "scoring_summary.json"
                    with open(summary_path, 'w') as sf:
                        json.dump(project_summary, sf, indent=2)
                except Exception as e:
                    logger.error(f"Failed to write scoring summary for {current_project_key}: {str(e)}")
                
            except Exception as e:
                logger.error(f"Error evaluating {current_project_key}: {str(e)}")
                scoring_results[current_project_key] = {
                    'status': 'error',
                    'error': str(e)
                }
                # Persist per-project scoring summary with found=0 on error
                try:
                    expected_findings = benchmark_map.get(current_project_key, [])
                    project_summary = {
                        'project': current_project_key,
                        'timestamp': '',
                        'expected': len(expected_findings),
                        'found': 0
                    }
                    summary_path = report_file.parent / "scoring_summary.json"
                    with open(summary_path, 'w') as sf:
                        json.dump(project_summary, sf, indent=2)
                except Exception as e2:
                    logger.error(f"Failed to write error scoring summary for {current_project_key}: {str(e2)}")

        successful_scorings = sum(1 for r in scoring_results.values() if r.get('status') == 'scored')
        failed_reports = sum(1 for r in scoring_results.values() if r.get('status') == 'failed')
        errors = sum(1 for r in scoring_results.values() if r.get('status') == 'error')
        no_benchmark = sum(1 for r in scoring_results.values() if r.get('status') == 'no_benchmark')

        logger.info(f"Evaluation complete: {successful_scorings} scored, {failed_reports} failed, {errors} errors, {no_benchmark} no benchmark data")

        return scoring_results

if __name__ == '__main__':
    local = settings.local
    logger.info(f"LOCAL: {local}")
    m = SandboxManager(is_local=local)
    m.run()
