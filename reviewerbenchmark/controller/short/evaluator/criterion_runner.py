"""Run exactly one criterion against one candidate file."""

import contextlib
import importlib.util
import io
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import checks


class _OutputLimit(RuntimeError):
    pass


class _LimitedText(io.StringIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, value):
        if self.tell() + len(value) > self.limit:
            raise _OutputLimit("criterion output limit exceeded")
        return super().write(value)


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: criterion_runner.py CRITERION SOLUTION")
    criterion, solution = sys.argv[1:]
    output = _LimitedText(65536)
    errors = _LimitedText(65536)
    try:
        if not hasattr(checks, criterion):
            raise KeyError(f"unknown criterion {criterion}")
        spec = importlib.util.spec_from_file_location("candidate_solution", solution)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load candidate")
        module = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            spec.loader.exec_module(module)
            getattr(checks, criterion)(module)
        result = {"criterion": criterion, "passed": True, "status": "pass"}
    except BaseException as error:
        result = {
            "criterion": criterion,
            "error_type": type(error).__name__,
            "message": str(error)[:500],
            "passed": False,
            "status": "fail"
        }
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
