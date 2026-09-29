"""
Logging for job entrypoints: INFO for the project's own loggers, WARNING for the rest.

Raising the root logger to INFO also turns on py4j and Spark, which flood the job log
(`Received command c on object id p0`).
"""

from __future__ import annotations

import logging

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure() -> None:
    logging.basicConfig(level=logging.WARNING, format=FORMAT)
    # "__main__": a module run with `python -m` logs under that name, not under src.*
    for name in ("src", "__main__"):
        logging.getLogger(name).setLevel(logging.INFO)
