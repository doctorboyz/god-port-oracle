"""Broky core configuration and utilities."""

import yaml
from pathlib import Path
from typing import Any


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load a YAML configuration file.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Dictionary of configuration values.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path) as f:
        return yaml.safe_load(f)


CONFIG_DIR = Path(__file__).parent.parent / "config"


def get_settings() -> dict[str, Any]:
    """Load the main Broky settings."""
    return load_config(CONFIG_DIR / "settings.yaml")


def get_indicators() -> dict[str, Any]:
    """Load indicator configuration."""
    return load_config(CONFIG_DIR / "indicators.yaml")


def get_risk() -> dict[str, Any]:
    """Load risk management configuration."""
    return load_config(CONFIG_DIR / "risk.yaml")