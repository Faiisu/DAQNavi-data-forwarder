#!/usr/bin/env python3
"""Clear a stopped DAQ spool outside the web server process."""

import json
import os
from pathlib import Path
import sys
import time

from production_acquisition import clear_spooled_data


def write_job(path, job_id, state, **details):
    record = {"job_id": job_id, "state": state, "checked_at_ns": time.time_ns(),
              **details}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temporary, path)


def main():
    directory = Path(sys.argv[1])
    max_bytes = int(sys.argv[2])
    job_id = sys.argv[3]
    job_path = directory / "buffer-clear-job.json"
    write_job(job_path, job_id, "running", pid=os.getpid())
    try:
        result = clear_spooled_data(directory, max_bytes)
    except Exception as exc:
        write_job(job_path, job_id, "failed", message=str(exc))
        raise
    write_job(job_path, job_id, "complete", **result)


if __name__ == "__main__":
    main()
