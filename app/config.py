"""Configuration management for w3-speak-shot."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class WindowGeometryConfig:
    """Persists and restores window position and size."""

    def __init__(self, config_path=None):
        """
        Initialize window geometry config.

        Args:
            config_path: Path to store geometry JSON. Defaults to app/.overlay_state.json
        """
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), ".overlay_state.json")
        self.config_path = config_path

    def load(self):
        """
        Load window geometry from disk.

        Returns:
            dict with keys x, y, width, height, or None if load fails
        """
        try:
            if not os.path.exists(self.config_path):
                logger.debug(f"Geometry config not found: {self.config_path}")
                return None

            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Validate keys
            required = {"x", "y", "width", "height"}
            if not required.issubset(data.keys()):
                logger.warning(f"Invalid geometry config: missing keys {required - set(data.keys())}")
                return None

            return {
                "x": int(data.get("x")),
                "y": int(data.get("y")),
                "width": int(data.get("width")),
                "height": int(data.get("height")),
            }
        except json.JSONDecodeError as e:
            logger.warning(f"Geometry config is invalid JSON: {e}")
            return None
        except (ValueError, KeyError) as e:
            logger.warning(f"Geometry config has invalid values: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error loading geometry config: {e}")
            return None

    def save(self, x, y, width, height):
        """
        Save window geometry to disk.

        Args:
            x, y: Window position
            width, height: Window dimensions
        """
        try:
            payload = {
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
            }
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            logger.debug(f"Saved geometry to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to save geometry config: {e}")
