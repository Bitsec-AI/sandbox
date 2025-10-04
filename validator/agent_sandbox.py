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
    print(f"[AGENT] Starting agent from file: {agent_file}")
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    resp = None

    try:
        with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
            spec = importlib.util.spec_from_file_location("agent", agent_file)
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)

            if not hasattr(agent, "agent_main"):
                raise AttributeError("agent.py does not define an 'agent_main()' function")

            result = agent.agent_main()
            print(f"[AGENT] agent_main() completed, result type: {type(result)}")

            try:
                pickle.dumps(result)
                resp = {"success": True, "report": result}
            except (pickle.PicklingError, AttributeError, TypeError) as e:
                tb_str = traceback.format_exc()
                resp = {"success": False, "error": f"Report serialization error: {e}: {tb_str}"}

    except SystemExit as e:
        resp = {"success": False, "error": f"Exited with code {e.code}"}

    except Exception as e:
        print(f"[AGENT] Exception: {e}")
        resp = {"success": False, "error": str(e)}

    # Capture stdout/stderr
    stdout_content = stdout_capture.getvalue()
    stderr_content = stderr_capture.getvalue()
    
    if resp is None:
        resp = {"success": False, "error": "No response generated"}

    resp.update({
        "stdout": stdout_content,
        "stderr": stderr_content,
    })
    
    print(f"[QUEUE] About to put result in queue: {resp.get('success', 'unknown')}")
    
    try:
        queue.put(resp, timeout=10)
        print(f"[QUEUE] Successfully put result in queue")
    except Exception as e:
        print(f"[QUEUE] ERROR putting result in queue: {e}")
        try:
            error_resp = {"success": False, "error": f"Queue put failed: {e}", "stdout": stdout_content, "stderr": stderr_content}
            queue.put(error_resp, timeout=5)
            print(f"[QUEUE] Put error response in queue")
        except Exception as e2:
            print(f"[QUEUE] CRITICAL: Could not put anything in queue: {e2}")
    
    print(f"[AGENT] Process completed")

def run_agent_direct(agent_file):
    """Run agent directly without multiprocessing for debugging."""
    print(f"[DIRECT] Running agent directly (no multiprocessing)")
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    resp = None

    try:
        with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
            spec = importlib.util.spec_from_file_location("agent", agent_file)
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)

            if not hasattr(agent, "agent_main"):
                raise AttributeError("agent.py does not define an 'agent_main()' function")

            result = agent.agent_main()
            print(f"[DIRECT] agent_main() completed, result type: {type(result)}")

            try:
                pickle.dumps(result)
                resp = {"success": True, "report": result}
            except (pickle.PicklingError, AttributeError, TypeError) as e:
                tb_str = traceback.format_exc()
                resp = {"success": False, "error": f"Report serialization error: {e}: {tb_str}"}

    except SystemExit as e:
        resp = {"success": False, "error": f"Exited with code {e.code}"}

    except Exception as e:
        print(f"[DIRECT] Exception: {e}")
        resp = {"success": False, "error": str(e)}

    # Capture stdout/stderr
    stdout_content = stdout_capture.getvalue()
    stderr_content = stderr_capture.getvalue()
    
    if resp is None:
        resp = {"success": False, "error": "No response generated"}

    resp.update({
        "stdout": stdout_content,
        "stderr": stderr_content,
    })
    
    print(f"[DIRECT] Execution completed: {resp.get('success', 'unknown')}")
    return resp

def run_with_timeout(agent_file, timeout_seconds=TIMEOUT_SECONDS):
    """Try direct execution first, fallback to multiprocessing."""
    print(f"[TIMEOUT] Attempting direct execution first...")
    
    try:
        resp = run_agent_direct(agent_file)
        print(f"[TIMEOUT] Direct execution successful: {resp.get('success', 'unknown')}")
        return resp
    except Exception as e:
        print(f"[TIMEOUT] Direct execution failed: {e}, falling back to multiprocessing")
    
    # Fallback to multiprocessing
    print(f"[TIMEOUT] Using multiprocessing fallback...")
    queue = multiprocessing.Queue()
    process = multiprocessing.Process(target=run_agent, args=(agent_file, queue))
    process.start()
    
    process.join(timeout_seconds)

    if process.is_alive():
        print(f"[TIMEOUT] Process timed out, terminating...")
        process.terminate()
        process.join()
        resp = {
            "success": False,
            "error": "Agent timeout",
        }
    else:
        print(f"[QUEUE] Getting result from queue...")
        try:
            resp = queue.get(timeout=5)
            print(f"[QUEUE] Got result from queue: {resp.get('success', 'unknown')}")
        except multiprocessing.queues.Empty:
            print(f"[QUEUE] Queue is empty, no result returned")
            resp = {
                "success": False,
                "error": "No result returned",
            }
        except Exception as e:
            print(f"[QUEUE] Error getting from queue: {e}")
            resp = {
                "success": False,
                "error": f"Queue get error: {e}",
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
