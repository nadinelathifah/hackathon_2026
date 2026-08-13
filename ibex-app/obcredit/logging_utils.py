"""Tiny logging helper so every module logs consistently and is debuggable.

Set OBCREDIT_LOG=DEBUG in the environment for verbose tracing of the f()
pipeline (stream detection, schedule fitting, per-feature values).
"""
from __future__ import annotations
import logging
import os

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        level = os.environ.get("OBCREDIT_LOG", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, level, logging.INFO),
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        )
        _CONFIGURED = True
    return logging.getLogger(name)
