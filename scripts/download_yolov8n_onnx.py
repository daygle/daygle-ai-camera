#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.request import urlretrieve

DEFAULT_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.onnx"


def main() -> None:
    parser = argparse.ArgumentParser(description="Download YOLOv8n ONNX model for Daygle AI Camera")
    parser.add_argument("--url", default=DEFAULT_URL, help="Model URL to download")
    parser.add_argument("--output", default="models/yolov8n.onnx", help="Destination ONNX path")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {args.url} -> {output}")
    urlretrieve(args.url, output)  # noqa: S310 - operator-provided model URL
    print(f"Saved {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
