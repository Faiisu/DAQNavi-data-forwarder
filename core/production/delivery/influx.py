"""InfluxDB line protocol encoding and production writer."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

def _lp_escape(value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("InfluxDB tag values cannot contain newline or carriage return")
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _lp_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n") + '"'


class InfluxProductionDestination:
    """InfluxDB 2.x line protocol sink; retries overwrite identical point identities."""

    def __init__(self, cfg, opener=None):
        self.cfg = cfg
        self.opener = opener or urllib.request.urlopen

    @staticmethod
    def points(rows, gaps, measurement):
        output = []
        for row in rows:
            tags = ",".join(f"{key}={_lp_escape(row[key])}" for key in (
                "device_id", "channel", "session_id", "unit", "provenance"))
            fields = {
                "sample_id": _lp_string(row["sample_id"]),
                "sensor_name": _lp_string(row["sensor_name"]),
                "raw_voltage": repr(float(row["raw_voltage"])),
                "calibrated_value": repr(float(row["calibrated_value"])),
            }
            encoded = ",".join(f"{_lp_escape(key)}={value}" for key, value in fields.items())
            output.append(f"{_lp_escape(measurement)},{tags} {encoded} {int(row['time_ns'])}")
        for gap in gaps:
            tags = f"gap_id={_lp_escape(gap['gap_id'])}"
            end = gap["end_ns"]
            fields = f"start_ns={int(gap['start_ns'])}i,open={str(end is None).lower()},cause={_lp_string(gap['cause'])}"
            if end is not None:
                fields += f",end_ns={int(end)}i"
            output.append(f"daq_acquisition_gaps,{tags} {fields} {int(gap['start_ns'])}")
        return "\n".join(output)

    def write(self, rows, gaps):
        payload = self.points(rows, gaps, self.cfg.INFLUX_MEASUREMENT)
        if not payload:
            return
        query = urllib.parse.urlencode({"org": self.cfg.INFLUX_ORG, "bucket": self.cfg.INFLUX_BUCKET, "precision": "ns"})
        url = f"{self.cfg.INFLUX_URL.rstrip('/')}/api/v2/write?{query}"
        headers = {"Authorization": f"Token {self.cfg.INFLUX_TOKEN}", "Content-Type": "text/plain; charset=utf-8",
                   "Accept": "application/json"}
        current, size = [], 0
        for line in payload.splitlines():
            line_size = len(line.encode()) + 1
            if current and size + line_size > 512 * 1024:
                self._post(url, "\n".join(current), headers)
                current, size = [], 0
            current.append(line)
            size += line_size
        if current:
            self._post(url, "\n".join(current), headers)

    def _post(self, url, payload, headers):
        request = urllib.request.Request(url, data=payload.encode(), method="POST", headers=headers)
        try:
            with self.opener(request, timeout=5) as response:
                if getattr(response, "status", 204) not in (204, 200):
                    raise RuntimeError(f"InfluxDB write returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"InfluxDB write returned HTTP {exc.code}") from exc
