import importlib.util
import multiprocessing
import os
import json
import io
import contextlib
import pickle
import traceback

TIMEOUT_SECONDS = 600
AGENT_FILE = os.getenv("AGENT_FILE", "/app/agent.py")
REPORT_FILE = os.getenv("REPORT_FILE", "/app/report.json")


def run_agent(agent_file, queue):
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    try:
        with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
            spec = importlib.util.spec_from_file_location("agent", agent_file)
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)

            if not hasattr(agent, "agent_main"):
                raise AttributeError("agent.py does not define an 'agent_main()' function")

            result = agent.agent_main()

            try:
                pickle.dumps(result)
            except (pickle.PicklingError, AttributeError, TypeError) as e:
                tb_str = traceback.format_exc()
                resp = {"success": False, "error": f"Report serialization error: {e}: {tb_str}"}
            else:
                resp = {"success": True, "report": result}

    except SystemExit as e:
        resp = {"success": False, "error": f"Exited with code {e.code}"}

    except Exception as e:
        resp = {"success": False, "error": str(e)}

    resp.update({
        "stdout": stdout_capture.getvalue(),
        "stderr": stderr_capture.getvalue(),
    })
    queue.put(resp)

def run_with_timeout(agent_file, timeout_seconds=TIMEOUT_SECONDS):
    queue = multiprocessing.Queue()
    process = multiprocessing.Process(target=run_agent, args=(agent_file, queue))
    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join()
        resp = {
            "success": False,
            "error": "Timeout",
        }

    try:
        resp = queue.get(timeout=0.1)
    except multiprocessing.queues.Empty:
        resp = {
            "success": False,
            "error": "No result returned",
        }

    resp.setdefault("stdout", "")
    resp.setdefault("stderr", "")

    return resp


if __name__ == "__main__":
    try:
        result = run_with_timeout(AGENT_FILE, TIMEOUT_SECONDS)

    except Exception as e:
        result = {
            "success": False,
            "error": f"Sandbox error",
            "exc": str(e),
            "traceback": traceback.format_exc(),
            "stdout": "",
            "stderr": "",
        }

    # Separate logs from the structured JSON report
    stdout_text = result.pop("stdout", "")
    stderr_text = result.pop("stderr", "")

    # Write structured report without stdout/stderr so it remains clean JSON
    with open(REPORT_FILE, "w") as f:
        json.dump(result, f, indent=2)

    # Persist logs alongside the report for debugging/traceability
    try:
        report_dir = os.path.dirname(REPORT_FILE) or "."
        report_stem = os.path.splitext(os.path.basename(REPORT_FILE))[0]
        stdout_path = os.path.join(report_dir, f"{report_stem}.stdout.log")
        stderr_path = os.path.join(report_dir, f"{report_stem}.stderr.log")

        with open(stdout_path, "w") as sf:
            sf.write(stdout_text or "")
        with open(stderr_path, "w") as ef:
            ef.write(stderr_text or "")
    except Exception:
        # Logging file creation should never break report writing
        pass
