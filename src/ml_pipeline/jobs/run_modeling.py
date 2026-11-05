"""
Job entrypoint for T2 · modeling (bundles/data/resources/data_pipeline_job.yml).

Same bundle-root lookup as src/data_pipeline/jobs/run_data_pipeline.py: a spark_python_task exec()s
this file, so `__file__` is not defined.
"""

import os
import sys
from pathlib import Path


def _bundle_root() -> Path:
    starts = [globals().get("__file__"), sys.argv[0] if sys.argv else None, os.getcwd()]
    for start in filter(None, starts):
        path = Path(start).resolve()
        for folder in (path, *path.parents):
            if (folder / "src" / "ml_pipeline").is_dir():
                return folder
    raise RuntimeError(f"Bundle root not found from {starts}")


sys.path.insert(0, str(_bundle_root()))

from src.ml_pipeline import pipeline  # noqa: E402

if __name__ == "__main__":
    pipeline.main()
