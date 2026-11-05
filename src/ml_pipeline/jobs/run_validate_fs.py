"""
Job entrypoint for the validate_fs task (bundles/data/resources/data_pipeline_job.yml).

Same bundle-root lookup as run_modeling.py: a spark_python_task exec()s this file.
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

from src.ml_pipeline import validate_fs  # noqa: E402

if __name__ == "__main__":
    validate_fs.main()
