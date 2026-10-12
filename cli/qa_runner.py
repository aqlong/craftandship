# cli/qa_runner.py - child process that runs model-written QA tests (v1 engine)

"""Executes a generated qa_tests.py in a process of its own.

cli/qa.py asks a model to write Playwright tests for an app and then has to
run that code. The code is steered by the customer's spec, so it is untrusted.
It used to be exec()'d inside the build worker, where it could read every
credential the worker holds. cli/qa.py now starts this file instead:

    python qa_runner.py <test_file> <base_url> <result_file>

with the scrubbed environment from deploy.env_without_secrets() (plus
DATABASE_URL for the throwaway test database, which the injected login helper
needs), the build directory as working directory, and a wall-clock timeout
after which the whole process group is killed.

What this is not: a sandbox. The child runs as the same user in the same
container as the worker, with the same filesystem and network. Keeping the
worker's secrets out of the child's environment and address space stops the
straightforward leaks (os.environ, a stack trace, a dependency that phones
home); it does not stop code that goes looking, for example in
/proc/<worker pid>/environ. See the known limitations in cli/README.md.

This module imports nothing from the rest of cli/ on purpose: those modules
call load_dotenv(), which would read a developer's .env back into the scrubbed
environment.
"""

import json
import sys


def execute_test_code(test_code: str, page, base_url: str) -> dict:
    """Run generated test code and return its raw results dict.

    The generated code must define run_qa_tests(page, base_url). Returns a
    dict with "passed" and "failed" lists (each item a dict with "name" and,
    for failures, "error"). Never raises for errors in the generated code.
    """
    namespace = {}
    try:
        exec(test_code, namespace)  # noqa: S102  # nosec B102  # the purpose of this child process: runs model-written tests outside the worker, started by cli/qa.py with a scrubbed environment
    except SyntaxError as exc:
        return {
            "passed": [],
            "failed": [{"name": "QA test compilation", "error": f"Syntax error in generated code: {exc}"}],
        }

    run_fn = namespace.get("run_qa_tests")
    if not run_fn:
        return {
            "passed": [],
            "failed": [{"name": "QA test runner", "error": "run_qa_tests not found in generated code"}],
        }

    try:
        result = run_fn(page, base_url)
        if not isinstance(result, dict):
            return {
                "passed": [],
                "failed": [{"name": "QA test runner", "error": f"run_qa_tests returned {type(result).__name__}, expected dict"}],
            }
        return result
    except Exception as exc:
        return {
            "passed": [],
            "failed": [{"name": "QA test execution", "error": str(exc)}],
        }


def main(argv) -> int:
    """Entry point of the child process. Writes the results as JSON to <result_file>."""
    if len(argv) != 4:
        print("usage: qa_runner.py <test_file> <base_url> <result_file>", file=sys.stderr)
        return 2
    test_file, base_url, result_file = argv[1], argv[2], argv[3]

    with open(test_file, encoding="utf-8") as handle:
        test_code = handle.read()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(20_000)
            page.set_default_navigation_timeout(20_000)
            raw_results = execute_test_code(test_code, page, base_url)
        finally:
            browser.close()

    with open(result_file, "w", encoding="utf-8") as handle:
        # default=str: generated code may put exceptions or Playwright objects
        # in its results; the parent only needs their text.
        json.dump(raw_results, handle, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
