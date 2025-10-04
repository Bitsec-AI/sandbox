import os
import time
import json
from pathlib import Path
from python_on_whales import docker, Network
from validator.projects import fetch_projects
from python_on_whales.exceptions import DockerException, NoSuchContainer, NoSuchNetwork
from python_on_whales.utils import run
from validator.scorer import ScaBenchScorerV2

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
        docker.build(
            self.proxy_dir,
            tags="bitsec-proxy:latest",
            build_contexts={"loggers": "loggers"},
        )
        docker.build(
            self.curr_dir,
            tags=SANDBOX_IMAGE_TAG,
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
        return reports_dir

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
            envs={
                "JOB_ID": job_id,
                "PROJECT_ID": project_id,
            },
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

    def eval_jobs(self, reports_dir, project_id=None):
        """
        Evaluate all reports in the reports directory using ScaBenchScorerV2.
        
        Args:
            reports_dir (str): Path to the directory containing report JSON files
            project_id (str, optional): Specific project ID to evaluate. If None, evaluates all projects.
            
        Returns:
            dict: Summary of scoring results for all projects
        """
        logger.info(f"Starting evaluation of reports in: {reports_dir}")
        if project_id:
            logger.info(f"Filtering to specific project: {project_id}")
        
        # Load benchmark data
        benchmark_file = os.path.join(self.curr_dir, 'curated-highs-only-2025-08-08.json')
        if not os.path.exists(benchmark_file):
            logger.error(f"Benchmark file not found: {benchmark_file}")
            return {}
        
        with open(benchmark_file, 'r') as f:
            benchmark_data = json.load(f)
        
        # Create a mapping of project_id to expected vulnerabilities
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
        
        # Find all report files
        reports_path = Path(reports_dir)
        if not reports_path.exists():
            logger.error(f"Reports directory does not exist: {reports_dir}")
            return {}
        
        # Look for report.json files in subdirectories
        if project_id:
            # Look for specific project report
            specific_report = reports_path / project_id / "report.json"
            if specific_report.exists():
                report_files = [specific_report]
            else:
                logger.error(f"Report not found for project {project_id}")
                return {}
        else:
            # Look for all report.json files
            report_files = list(reports_path.glob("*/report.json"))
        
        if not report_files:
            logger.warning(f"No report.json files found in {reports_dir}")
            return {}
        
        logger.info(f"Found {len(report_files)} report files to evaluate")
        
        scoring_results = {}
        
        for report_file in report_files:
            current_project_id = report_file.parent.name
            logger.info(f"Evaluating project: {current_project_id}")
            
            try:
                # Load the report
                with open(report_file, 'r') as f:
                    report_data = json.load(f)
                
                # Check if the report contains successful findings
                if not report_data.get('success', False):
                    logger.warning(f"Report for {current_project_id} indicates failure: {report_data.get('error', 'Unknown error')}")
                    # Create a mock result for failed reports
                    scoring_results[current_project_id] = {
                        'status': 'failed',
                        'error': report_data.get('error', 'Unknown error'),
                        'stdout': report_data.get('stdout', ''),
                        'stderr': report_data.get('stderr', '')
                    }
                    # Persist per-project scoring summary with found=0 on failure
                    try:
                        expected_findings = benchmark_map.get(current_project_id, [])
                        project_summary = {
                            'project': current_project_id,
                            'timestamp': report_data.get('report', {}).get('timestamp') or '',
                            'expected': len(expected_findings),
                            'found': 0
                        }
                        summary_path = report_file.parent / "scoring_summary.json"
                        with open(summary_path, 'w') as sf:
                            json.dump(project_summary, sf, indent=2)
                    except Exception as e:
                        logger.error(f"Failed to write failure scoring summary for {current_project_id}: {str(e)}")
                    continue
                
                # Extract findings from the report
                tool_findings = report_data.get('findings', [])
                
                # If no findings at top level, check under 'report.vulnerabilities'
                if not tool_findings and 'report' in report_data:
                    tool_findings = report_data['report'].get('vulnerabilities', [])
                
                if not tool_findings:
                    logger.warning(f"No findings found in report for {current_project_id}")
                    scoring_results[current_project_id] = {
                        'status': 'no_findings',
                        'message': 'No findings reported by the tool'
                    }
                    continue
                
                # Get expected vulnerabilities from benchmark data
                expected_findings = benchmark_map.get(current_project_id, [])
                
                if not expected_findings:
                    logger.warning(f"No benchmark data found for project {current_project_id}")
                    scoring_results[current_project_id] = {
                        'status': 'no_benchmark',
                        'message': f'No benchmark data available for project {current_project_id}',
                        'tool_findings_count': len(tool_findings)
                    }
                    continue
                
                logger.info(f"Scoring {current_project_id} with {len(expected_findings)} expected vulnerabilities and {len(tool_findings)} tool findings")
                
                # Score the project
                result = scorer.score_project(
                    expected_findings=expected_findings,
                    tool_findings=tool_findings,
                    project_name=current_project_id
                )
                
                # Store the scoring result
                scoring_results[current_project_id] = {
                    'status': 'scored',
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
                        'undecided_findings': result.undecided_findings,
                        'extra_findings': result.extra_findings
                    }
                }
                
                # Concise summary for CI/logs: true positives vs expected
                logger.info(
                    f"Scoring Project: {current_project_id} | Found: {result.true_positives} | Expected: {result.total_expected}"
                )

                # Persist per-project scoring summary next to report.json
                try:
                    project_summary = {
                        'project': current_project_id,
                        'timestamp': result.timestamp,
                        'expected': result.total_expected,
                        'found': result.true_positives
                    }
                    summary_path = report_file.parent / "scoring_summary.json"
                    with open(summary_path, 'w') as sf:
                        json.dump(project_summary, sf, indent=2)
                except Exception as e:
                    logger.error(f"Failed to write scoring summary for {current_project_id}: {str(e)}")
                
            except Exception as e:
                logger.error(f"Error evaluating {current_project_id}: {str(e)}")
                scoring_results[current_project_id] = {
                    'status': 'error',
                    'error': str(e)
                }
                # Persist per-project scoring summary with found=0 on error
                try:
                    expected_findings = benchmark_map.get(current_project_id, [])
                    project_summary = {
                        'project': current_project_id,
                        'timestamp': '',
                        'expected': len(expected_findings),
                        'found': 0
                    }
                    summary_path = report_file.parent / "scoring_summary.json"
                    with open(summary_path, 'w') as sf:
                        json.dump(project_summary, sf, indent=2)
                except Exception as e2:
                    logger.error(f"Failed to write error scoring summary for {current_project_id}: {str(e2)}")
        
        # Log summary
        successful_scorings = sum(1 for r in scoring_results.values() if r.get('status') == 'scored')
        failed_reports = sum(1 for r in scoring_results.values() if r.get('status') == 'failed')
        errors = sum(1 for r in scoring_results.values() if r.get('status') == 'error')
        no_benchmark = sum(1 for r in scoring_results.values() if r.get('status') == 'no_benchmark')
        
        logger.info(f"Evaluation complete: {successful_scorings} scored, {failed_reports} failed, {errors} errors, {no_benchmark} no benchmark data")
        
        # # Persist aggregate summary at reports root
        # try:
        #     aggregate = {}
        #     for pid, res in scoring_results.items():
        #         if res.get('status') == 'scored':
        #             agg_res = res.get('report', {})
        #             aggregate[pid] = {
        #                 'expected': agg_res.get('total_expected', 0),
        #                 'found': agg_res.get('true_positives', 0),
        #                 'timestamp': agg_res.get('timestamp')
        #             }
        #     aggregate_path = Path(reports_dir) / "scoring_summary.json"
        #     with open(aggregate_path, 'w') as af:
        #         json.dump(aggregate, af, indent=2)
        # except Exception as e:
        #     logger.error(f"Failed to write aggregate scoring summary: {str(e)}")
        
        return scoring_results

if __name__ == '__main__':
    m = SandboxManager()
    # m.run()

    
    # Evaluate all reports using ScaBenchScorerV2
    # score = m.eval_jobs(reports_dir)
    
    # Or evaluate a specific project (uncomment and modify the project_id)
    # score = m.eval_jobs(reports_dir, project_id="code4rena_superposition_2025_01")
    score = m.eval_jobs(reports_dir)
    
    print(f"Scoring results: {score}")