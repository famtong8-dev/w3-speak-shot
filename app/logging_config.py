"""Logging configuration for w3-speak-shot."""

import logging
import sys


def setup_logging():
    """Configure logging with a standard format."""
    logger = logging.getLogger("w3speak")

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def get_logger(name):
    """Get a logger instance for the given name."""
    return logging.getLogger(f"w3speak.{name}")
