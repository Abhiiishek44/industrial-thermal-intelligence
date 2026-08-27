"""Explicit offline entry point; never imported by normal application startup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.training_data.builder import build_training_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Build evidence-backed thermal training tables")
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    manifest = build_training_dataset(args.repository_root, output_root=args.output_root)
    print(json.dumps({
        "manifest_path": manifest["manifest_path"],
        "split_counts": manifest["split_counts"],
        "label_summary": manifest["label_summary"],
    }, indent=2))


if __name__ == "__main__":
    main()
