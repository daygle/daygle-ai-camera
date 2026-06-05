#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from ultralytics import YOLO


def main() -> None:
    parser = argparse.ArgumentParser(description="Export YOLOv8n to ONNX for Daygle AI Camera")
    parser.add_argument("--output", default="models/yolov8n.onnx", help="Destination ONNX path")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Exporting yolov8n.pt -> {output}")
    exported_path = Path(YOLO("yolov8n.pt").export(format="onnx"))
    if exported_path.resolve() != output.resolve():
        exported_path.replace(output)
    print(f"Saved {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
