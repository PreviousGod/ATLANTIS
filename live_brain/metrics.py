"""Simple metrics logging for live_brain health monitoring."""
import json
import time
from pathlib import Path


class MetricsLogger:
    def __init__(self, db_path: str):
        self.metrics_file = Path(db_path).parent / 'metrics.jsonl'

    def log(self, metric_type: str, value, tags: dict = None):
        """Append metric to JSONL file."""
        entry = {
            'timestamp': time.time(),
            'type': metric_type,
            'value': value,
            'tags': tags or {}
        }
        with open(self.metrics_file, 'a') as f:
            f.write(json.dumps(entry) + '\n')
