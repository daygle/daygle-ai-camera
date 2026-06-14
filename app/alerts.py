from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any


class AlertEngine:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules
        self.last_triggered: dict[str, float] = {}
        self._lock = threading.Lock()

    def process(self, detections: list[dict[str, Any]], rules: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        effective_rules = rules if rules is not None else self.rules
        alerts: list[dict[str, Any]] = []
        motion_detections = [detection for detection in detections if detection.get('motion_event')]
        for motion_detection in motion_detections:
            self._append_motion_alerts(alerts, motion_detection, effective_rules)

        for detection in detections:
            label = detection.get('label')
            if not isinstance(label, str) or not label:
                continue
            label_key = self._normalize_object_label(label)
            confidence = float(detection.get('confidence', 0))

            for rule in effective_rules:
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

                now = time.time()
                with self._lock:
                    last = self.last_triggered.get(rule_name, 0)
                    if now - last < cooldown:
                        continue
                    self.last_triggered[rule_name] = now

                alerts.append({
                    'rule_name': rule_name,
                    'label': label,
                    'confidence': confidence,
                    'message': f'Alert triggered: {label} detected ({confidence:.2%})'
                })

        return alerts

    def _append_motion_alerts(self, alerts: list[dict[str, Any]], detection: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> None:
        confidence = float(detection.get('confidence', 0))
        detection_zone_id = str(detection.get('zone_id') or '').strip()
        for rule in (rules if rules is not None else self.rules):
            if not rule.get('enabled', True) or not self._is_motion_rule(rule):
                continue
            if not self._is_active_now(rule):
                continue
            if confidence < float(rule.get('min_confidence', 0.0)):
                continue
            # Mirror the zone-id check used for object rules (line 43-46) so
            # a motion rule scoped to Zone A doesn't fire when motion is only
            # in Zone B.
            rule_zone_id = str(rule.get('zone_id') or '').strip()
            if rule_zone_id and rule_zone_id != detection_zone_id:
                continue

            rule_name = str(rule.get('name') or 'Motion')
            cooldown = int(rule.get('cooldown_seconds', 60))
            now = time.time()
            with self._lock:
                last = self.last_triggered.get(rule_name, 0)
                if now - last < cooldown:
                    continue
                self.last_triggered[rule_name] = now
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

    @staticmethod
    def _is_active_now(rule: dict[str, Any]) -> bool:
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
