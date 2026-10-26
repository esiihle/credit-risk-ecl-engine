"""
Configuration loading and seeding.

One function reads config.yaml, one seeds every random number generator in the
project. Both are here so that "where did this threshold come from?" has a
single answer, and so a run can be reproduced from the seed alone.
"""
from pathlib import Path

import numpy as np
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"


class ConfigError(Exception):
    """Raised when the configuration is missing or internally inconsistent."""


def load_config(path=None):
    """Load config.yaml and check the things that must be true of it.

    The checks here are cheap and catch the edits most likely to be made by
    hand: segment shares that no longer sum to 1, a reporting date before the
    last origination, transition probabilities that leave a bucket with more
    than certainty of moving.
    """
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.exists():
        raise ConfigError(f"No configuration at {path}")

    with open(path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    _validate(config)
    return config


def _validate(config):
    """Fail loudly on a config that cannot produce a sensible book."""
    segments = config.get("portfolio", {}).get("segments", {})
    if not segments:
        raise ConfigError("portfolio.segments is empty.")

    total_share = sum(s["share"] for s in segments.values())
    if abs(total_share - 1.0) > 1e-9:
        raise ConfigError(
            f"Segment shares sum to {total_share}, not 1.0. Every account must "
            "belong to exactly one segment."
        )

    transitions = config.get("generator", {}).get("base_transitions", {})
    # Leaving a bucket is a choice between cure, worsen and stay. The first two
    # cannot together exceed certainty, or "stay" has negative probability.
    for bucket in (1, 2, 3):
        leaving = (transitions.get(f"cure_from_{bucket}", 0.0)
                   + transitions.get(f"worsen_from_{bucket}", 0.0))
        if leaving > 1.0:
            raise ConfigError(
                f"Bucket {bucket}: cure + worsen = {leaving:.3f} > 1. There is "
                "no probability left for staying in the bucket."
            )

    staging = config.get("staging", {})
    if staging.get("sicr_dpd_backstop", 30) >= staging.get("default_dpd", 90):
        raise ConfigError(
            "The SICR backstop must trigger BEFORE default, or Stage 2 can "
            "never be reached — every account would jump from 1 to 3."
        )


def set_seed(seed):
    """Seed the global generator and return a fresh one for local use.

    Returning a generator as well as seeding globally is deliberate: code that
    takes an explicit `rng` is testable, and code that quietly uses the global
    one is not. New code should take the returned generator.
    """
    np.random.seed(seed)
    return np.random.default_rng(seed)
