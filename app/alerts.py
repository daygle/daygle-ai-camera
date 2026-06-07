from __future__ import annotations

import time
from datetime import datetime
from typing import Any


class AlertEngine:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules
        self.last_triggered: dict[str, float] = {}

    def process(self, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        motion_detections = [detection for detection in detections if detection.get('motion_event')]
        if motion_detections:
            strongest_motion = max(motion_detections, key=lambda detection: float(detection.get('confidence', 0)))
            self._append_motion_alerts(alerts, strongest_motion)

        for detection in detections:
            label_value = detection.get('label')
            if not isinstance(label_value, str) or not label_value:
                continue
            label = label_value
            label_key = self._normalize_object_label(label)
            confidence = float(detection.get('confidence', 0))

            for rule in self.rules:
                if not rule.get('enabled', True):
                    continue

                if self._is_motion_rule(rule):
                    continue

                if not self._is_active_now(rule):
                    continue

                if self._normalize_object_label(rule.get('object')) != label_key:
                    continue

                rule_zone_id = str(rule.get('zone_id') or '').strip()
                detection_zone_id = str(detection.get('zone_id') or '').strip()
                if rule_zone_id and rule_zone_id != detection_zone_id:
                    continue

                if confidence < float(rule.get('min_confidence', 0.5)):
                    continue

                rule_name = str(rule.get('name') or label)
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

    def _append_motion_alerts(self, alerts: list[dict[str, Any]], detection: dict[str, Any]) -> None:
        confidence = float(detection.get('confidence', 0))
        for rule in self.rules:
            if not rule.get('enabled', True) or not self._is_motion_rule(rule):
                continue
            if not self._is_active_now(rule):
                continue
            if confidence < float(rule.get('min_confidence', 0.0)):
                continue

            rule_name = str(rule.get('name') or 'Motion')
            cooldown = int(rule.get('cooldown_seconds', 60))
            last = self.last_triggered.get(rule_name, 0)
            if time.time() - last < cooldown:
                continue

            self.last_triggered[rule_name] = time.time()
            alerts.append({
                'rule_name': rule_name,
                'label': 'motion',
                'confidence': confidence,
                'message': f'Alert triggered: motion detected ({confidence:.2%})',
            })

    @staticmethod
    def _is_motion_rule(rule: dict[str, Any]) -> bool:
        return AlertEngine._normalize_object_label(rule.get('object')) == 'motion'

    @staticmethod
    def _normalize_object_label(value: Any) -> str:
        label = str(value or '').strip().lower()
        aliases = {
            'human': 'person',
            'people': 'person',
            'pedestrian': 'person',
        }
        return aliases.get(label, label)

    def _is_active_now(self, rule: dict[str, Any]) -> bool:
        start = rule.get('active_start')
        end = rule.get('active_end')
        if not start or not end:
            return True
        now = datetime.now().strftime('%H:%M')
        start_text = str(start)
        end_text = str(end)
        if start_text <= end_text:
            return start_text <= now <= end_text
        return now >= start_text or now <= end_text
