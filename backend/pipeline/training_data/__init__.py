"""Versioned, classifier-agnostic thermal-source training datasets."""

from pipeline.training_data.builder import build_training_dataset
from pipeline.training_data.regions import TRAINING_REGIONS, get_training_region

__all__ = ["TRAINING_REGIONS", "build_training_dataset", "get_training_region"]
