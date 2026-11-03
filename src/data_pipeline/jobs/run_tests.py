"""
Job entrypoint for the `unit_tests` task (bundles/data/resources/data_pipeline_job.yml).

Runs the Spark tests on the job cluster until CI exists (plan, Phase 2). A failed test fails the
task, so data_pipeline does not run. Same bundle-root lookup as run_data_pipeline.py.
"""

import os
import sys
from pathlib import Path


def _bundle_root() -> Path:
    starts = [globals().get("__file__"), sys.argv[0] if sys.argv else None, os.getcwd()]
    for start in filter(None, starts):
        path = Path(start).resolve()
        for folder in (path, *path.parents):
            if (folder / "src" / "data_pipeline").is_dir():
                return folder
    raise RuntimeError(f"Bundle root not found from {starts}")


root = _bundle_root()
sys.path.insert(0, str(root))

import pytest  # noqa: E402

if __name__ == "__main__":
    # -p no:cacheprovider: the bundle folder is read-only for the job's service principal.
    code = pytest.main([str(root / "tests"), "-v", "-p", "no:cacheprovider"])
    if code != 0:
        raise SystemExit(code)
