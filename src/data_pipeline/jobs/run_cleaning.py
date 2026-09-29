"""
Job entrypoint for Stage 2 · Cleaning (bundles/data/resources/ingestion_job.yml, task `cleaning`).

Same mechanics as run_ingestion.py: a spark_python_task exec()s this file, so the bundle root is
found by walking up from the script path (sys.argv[0]) or the working directory until
`src/data_pipeline` shows up, and is added to sys.path so `src.` imports resolve.
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


sys.path.insert(0, str(_bundle_root()))

from src.data_pipeline import cleaning  # noqa: E402

if __name__ == "__main__":
    cleaning.main()
