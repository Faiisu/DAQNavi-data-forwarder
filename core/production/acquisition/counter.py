"""Count calibrated threshold crossings in configurable acquisition windows."""

from __future__ import annotations


SECOND_NS = 1_000_000_000


class CycleCounter:
    def __init__(self, cfg, session_id):
        self.cfg = cfg
        self.session_id = session_id
        self.states = {}
        for index, channel in cfg.channels.items():
            if channel.enabled and channel.counter_enabled:
                self.states[index] = {"channel": channel, "bucket": None,
                                      "window_ns": channel.counter_interval_seconds * SECOND_NS,
                                      "armed": False, "count": 0, "total": 0}

    def add_samples(self, rows):
        """Return completed windows; the caller commits these with the source samples."""
        completed = []
        for row in rows:
            index = row["channel"]
            state = self.states.get(index)
            if state is None:
                continue
            bucket = int(row["time_ns"]) // state["window_ns"]
            if state["bucket"] is not None and bucket != state["bucket"]:
                # Fill elapsed windows, including those with zero crossings.
                for window in range(state["bucket"], bucket):
                    completed.append(self._record(index, state, window))
                    state["count"] = 0
            state["bucket"] = bucket
            channel = state["channel"]
            value = row["calibrated_value"]
            threshold = channel.counter_threshold
            if channel.counter_direction == "up":
                if value < threshold:
                    state["armed"] = True
                elif value >= threshold and state["armed"]:
                    state["count"] += 1
                    state["total"] += 1
                    state["armed"] = False
            else:
                if value > threshold:
                    state["armed"] = True
                elif value <= threshold and state["armed"]:
                    state["count"] += 1
                    state["total"] += 1
                    state["armed"] = False
        return completed

    def _record(self, index, state, window):
        channel = state["channel"]
        return {
            "record_type": "cycle_count",
            "time_ns": (window + 1) * state["window_ns"],
            "sample_id": f"{self.session_id}:count:{index}:{window}",
            "session_id": self.session_id,
            "device_id": self.cfg.DEVICE_ID,
            "channel": index,
            "sensor_name": channel.label,
            "direction": channel.counter_direction,
            "threshold": channel.counter_threshold,
            "interval_seconds": channel.counter_interval_seconds,
            "cycle_count": state["count"],
            "total_count": state["total"],
            "provenance": "physical_daq",
        }
