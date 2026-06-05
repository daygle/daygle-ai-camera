from __future__ import annotations

import time
from typing import Any


class AlertEngine:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules
        self.last_triggered: dict[str, float] = {}

    def process(self, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []

        for detection in detections:
            label = detection.get('label')
            confidence = float(detection.get('confidence', 0))

            for rule in self.rules:
                if not rule.get('enabled', True):
                    continue

                if rule.get('object') != label:
                    continue

                if confidence < float(rule.get('min_confidence', 0.5)):
                    continue

                rule_name = rule.get('name', label)
                cooldown = int(rule.get('cooldown_seconds', 60))

                last = self.last_triggered.get(rule_name, 0)
                if time.time() - last < cooldown:
                    continue

                self.last_triggered[rule_name] = time.time()

                alerts.append({
                    'rule_name': rule_name,
                    'label': label,
                    'confidence': confidence,
                    'message': f'Alert triggered: {label} detected ({confidence:.2%})'
                })

        return alerts
