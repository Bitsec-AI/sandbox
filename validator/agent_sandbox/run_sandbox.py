import importlib.util
import multiprocessing
import os
import json
import io
import logging
import contextlib
import pickle
import traceback
import tempfile
import shutil

TIMEOUT_SECONDS = 30 * 60
AGENT_FILE = os.getenv("AGENT_FILE", "/app/agent.py")
REPORT_FILE = os.getenv("REPORT_FILE", "/app/report.json")
QUEUE_TIMEOUT = 30  # Timeout for queue operations
MAX_QUEUE_SIZE = 65345  # 63.8KB - exact threshold found through testing
FORCE_MULTIPROCESSING = os.getenv("FORCE_MULTIPROCESSING", "true").lower() == "true"

logger = logging.getLogger(__name__)


def get_result_size(result):
    """Estimate the size of a result object in bytes."""
    try:
        return len(pickle.dumps(result))
    except Exception:
        return len(json.dumps(result, default=str))


def save_large_result_to_file(result, temp_dir):
    """Save large result to a temporary file and return the file path."""
    try:
        # Create a temporary file
        fd, temp_file = tempfile.mkstemp(suffix=".json", dir=temp_dir)
        os.close(fd)

        # Write result to file
        with open(temp_file, "w") as f:
            json.dump(result, f, indent=2)

        return temp_file
    except Exception as e:
        logger.error(f"[FILE] Error saving result to file: {e}")
        return None


def load_result_from_file(file_path):
    """Load result from a temporary file."""
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[FILE] Error loading result from file: {e}")
        return None


def init_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def run_agent(agent_file, queue, temp_dir):
    init_logging()

    logger.info(f"[AGENT] Starting agent from file: {agent_file}")
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    resp = None

    try:
        with (
            contextlib.redirect_stdout(stdout_capture),
            contextlib.redirect_stderr(stderr_capture),
        ):
            logger.info("[AGENT] Loading agent module...")
            spec = importlib.util.spec_from_file_location("agent", agent_file)
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)

            if not hasattr(agent, "agent_main"):
                raise AttributeError("agent.py does not define an 'agent_main()' function")

            logger.info("[AGENT] Starting agent_main() execution...")
            result = agent.agent_main()
            logger.info(f"[AGENT] agent_main() completed, result type: {type(result)}")

            try:
                pickle.dumps(result)
                resp = {"success": True, "report": result}
            except (pickle.PicklingError, AttributeError, TypeError) as e:
                tb_str = traceback.format_exc()
                resp = {
                    "success": False,
                    "error": f"Report serialization error: {e}: {tb_str}",
                }

    except SystemExit as e:
        logger.error(f"[AGENT] Exiting with code: {e.code}")
        resp = {"success": False, "error": f"Exited with code {e.code}"}

    except Exception as e:
        logger.error(f"[AGENT] Exception: {e}")
        resp = {"success": False, "error": str(e)}

    # Capture stdout/stderr
    stdout_content = stdout_capture.getvalue()
    stderr_content = stderr_capture.getvalue()

    if resp is None:
        resp = {"success": False, "error": "No response generated"}

    resp.update(
        {
            "stdout": stdout_content,
            "stderr": stderr_content,
        }
    )

    logger.info(f"[QUEUE] About to put result in queue: {resp.get('success', 'unknown')}")

    # Check if result is too large for queue
    result_size = get_result_size(resp)
    logger.info(f"[QUEUE] Result size: {result_size} bytes")

    if result_size > MAX_QUEUE_SIZE:
        logger.info(f"[QUEUE] Result too large ({result_size} bytes), saving to file")
        temp_file = save_large_result_to_file(resp, temp_dir)
        if temp_file:
            # Send file path instead of full result
            file_resp = {
                "success": True,
                "result_file": temp_file,
                "result_size": result_size,
                "stdout": stdout_content,
                "stderr": stderr_content,
            }
            try:
                queue.put(file_resp, timeout=QUEUE_TIMEOUT)
                logger.info(f"[QUEUE] Successfully put file path in queue: {temp_file}")

            except Exception as e:
                logger.error(f"[QUEUE] ERROR putting file path in queue: {e}")
                # Clean up temp file
                try:
                    os.unlink(temp_file)
                except Exception:
                    pass
        else:
            logger.info("[QUEUE] Failed to save result to file")
            error_resp = {
                "success": False,
                "error": "Failed to save large result to file",
                "stdout": stdout_content,
                "stderr": stderr_content,
            }
            try:
                queue.put(error_resp, timeout=QUEUE_TIMEOUT)
            except Exception as e:
                logger.error(f"[QUEUE] CRITICAL: Could not put error response in queue: {e}")
    else:
        # Result is small enough for queue
        try:
            queue.put(resp, timeout=QUEUE_TIMEOUT)
            logger.info("[QUEUE] Successfully put result in queue")
        except Exception as e:
            logger.error(f"[QUEUE] ERROR putting result in queue: {e}")
            try:
                error_resp = {
                    "success": False,
                    "error": f"Queue put failed: {e}",
                    "stdout": stdout_content,
                    "stderr": stderr_content,
                }
                queue.put(error_resp, timeout=QUEUE_TIMEOUT)
                logger.info("[QUEUE] Put error response in queue")
            except Exception as e2:
                logger.error(f"[QUEUE] CRITICAL: Could not put anything in queue: {e2}")

    logger.info("[AGENT] Process completed")


def run_agent_direct(agent_file):
    """Run agent directly without multiprocessing for debugging."""
    logger.info("[DIRECT] Running agent directly (no multiprocessing)")
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    resp = None

    try:
        with (
            contextlib.redirect_stdout(stdout_capture),
            contextlib.redirect_stderr(stderr_capture),
        ):
            spec = importlib.util.spec_from_file_location("agent", agent_file)
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)

            if not hasattr(agent, "agent_main"):
                raise AttributeError("agent.py does not define an 'agent_main()' function")

            result = agent.agent_main()
            logger.info(f"[DIRECT] agent_main() completed, result type: {type(result)}")

            try:
                pickle.dumps(result)
                resp = {"success": True, "report": result}
            except (pickle.PicklingError, AttributeError, TypeError) as e:
                tb_str = traceback.format_exc()
                resp = {
                    "success": False,
                    "error": f"Report serialization error: {e}: {tb_str}",
                }

    except SystemExit as e:
        logger.error(f"[AGENT] Exiting with code: {e.code}")
        resp = {"success": False, "error": f"Exited with code {e.code}"}

    except Exception as e:
        logger.error(f"[DIRECT] Exception: {e}")
        resp = {"success": False, "error": str(e)}

    # Capture stdout/stderr
    stdout_content = stdout_capture.getvalue()
    stderr_content = stderr_capture.getvalue()

    if resp is None:
        resp = {"success": False, "error": "No response generated"}

    resp.update(
        {
            "stdout": stdout_content,
            "stderr": stderr_content,
        }
    )

    logger.info(f"[DIRECT] Execution completed: {resp.get('success', 'unknown')}")
    return resp


def run_with_timeout(agent_file, timeout_seconds=TIMEOUT_SECONDS):
    """Try direct execution first, fallback to multiprocessing."""
    if FORCE_MULTIPROCESSING:
        logger.info("[TIMEOUT] FORCE_MULTIPROCESSING=true, skipping direct execution")
    else:
        logger.info("[TIMEOUT] Attempting direct execution first...")

        try:
            resp = run_agent_direct(agent_file)
            logger.info(f"[TIMEOUT] Direct execution successful: {resp.get('success', 'unknown')}")
            return resp
        except Exception as e:
            logger.error(f"[TIMEOUT] Direct execution failed: {e}, falling back to multiprocessing")

    # Fallback to multiprocessing with file-based communication for large results
    logger.info("[TIMEOUT] Using multiprocessing fallback...")

    # Create temporary directory for large results
    temp_dir = tempfile.mkdtemp(prefix="agent_sandbox_")
    logger.info(f"[TIMEOUT] Created temp directory: {temp_dir}")

    try:
        queue = multiprocessing.Queue()
        process = multiprocessing.Process(target=run_agent, args=(agent_file, queue, temp_dir))

        logger.info(f"[TIMEOUT] Starting multiprocessing with {timeout_seconds}s timeout...")
        process.start()

        logger.info("[TIMEOUT] Waiting for process to complete...")
        process.join(timeout_seconds)

        # FIX: Check queue FIRST before checking is_alive() to prevent false timeouts.
        #
        # Race condition explained:
        # When process.join(timeout) returns, either the process finished OR the timeout
        # elapsed - we don't know which. The old code checked is_alive() first, which
        # could return True briefly even if the process just finished (during cleanup),
        # causing false "Agent timeout" errors when the result was actually in the queue.
        #
        # The fix: Always try to get from queue first with a short timeout (1s).
        # This handles the case where the result is mid-flush to the pipe when join()
        # returns. Using get(timeout=1) instead of get_nowait() eliminates the race
        # window where the result is being pickled/flushed but not yet available.
        # The 1-second grace period is negligible compared to the 20-minute timeout.
        #
        # Only if the queue is empty after the grace period do we check is_alive()
        # to determine if this is a real timeout or a process that crashed.
        logger.info("[QUEUE] Checking queue for result...")
        try:
            resp = queue.get(timeout=1)  # Short timeout to handle in-flight results
            logger.info(f"[QUEUE] Got result from queue: {resp.get('success', 'unknown')}")

            # Check if result is stored in a file (for large results)
            if resp.get("success") and "result_file" in resp:
                result_file = resp["result_file"]
                logger.info(f"[FILE] Loading large result from file: {result_file}")
                file_result = load_result_from_file(result_file)
                if file_result:
                    resp = file_result
                    logger.info("[FILE] Successfully loaded result from file")
                else:
                    resp = {
                        "success": False,
                        "error": "Failed to load result from file",
                    }

                # Clean up temp file
                try:
                    os.unlink(result_file)
                except Exception:
                    pass

        except multiprocessing.queues.Empty:
            # No result in queue - determine why
            if process.is_alive():
                # Process still running after timeout - this is a real timeout
                logger.error(f"[TIMEOUT] Process timed out after {timeout_seconds}s, terminating...")
                process.terminate()
                process.join(timeout=5)
                # If terminate didn't work, force kill
                if process.is_alive():
                    logger.error("[TIMEOUT] Process did not terminate, killing...")
                    process.kill()
                    process.join()
                resp = {
                    "success": False,
                    "error": "Agent timeout",
                }
            else:
                # Process exited but didn't put result in queue - likely crashed
                logger.error("[QUEUE] Process exited but queue is empty, no result returned")
                resp = {
                    "success": False,
                    "error": "No result returned",
                }
        except (BrokenPipeError, EOFError) as e:
            # Queue communication failed - process may have crashed
            logger.error(f"[QUEUE] Queue communication error: {e}")
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join()
            resp = {
                "success": False,
                "error": f"Queue communication error: {e}",
            }
        except Exception as e:
            logger.error(f"[QUEUE] Error getting from queue: {e}")
            resp = {
                "success": False,
                "error": f"Queue get error: {e}",
            }

        resp.setdefault("stdout", "")
        resp.setdefault("stderr", "")

        return resp

    finally:
        # Clean up temporary directory
        try:
            shutil.rmtree(temp_dir)
            logger.info(f"[TIMEOUT] Cleaned up temp directory: {temp_dir}")
        except Exception as e:
            logger.error(f"[TIMEOUT] Warning: Could not clean up temp directory {temp_dir}: {e}")


if __name__ == "__main__":
    init_logging()

    try:
        result = run_with_timeout(AGENT_FILE, TIMEOUT_SECONDS)

    except Exception as e:
        result = {
            "success": False,
            "error": "Sandbox error",
            "exc": str(e),
            "traceback": traceback.format_exc(),
            "stdout": "",
            "stderr": "",
        }

    # Separate logs from the structured JSON report
    stdout_text = result.get("stdout", "")
    stderr_text = result.get("stderr", "")

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
