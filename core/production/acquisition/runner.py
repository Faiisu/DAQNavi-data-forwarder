"""Coordinate DAQ capture, durable replay, and shutdown."""

from __future__ import annotations

from collections import deque
import logging
from pathlib import Path
import threading
import time

from ..config import validate_production_config
from ..delivery.identity import destination_identity
from ..delivery.influx import InfluxProductionDestination
from ..delivery.mqtt import MQTTProductionDestination
from ..delivery.timescale import TimescaleProductionDestination
from ..fault import AcquisitionFault
from ..spool import DurableSpool
from .daq import AdvantechDaq
from .pipeline import ProductionPipeline

log = logging.getLogger(__name__)
_WRITER_BATCH_GROUP = 4
_WRITER_MAX_ROWS = 8000

def run_production(cfg, stop_event=None, daq_factory=AdvantechDaq, destination=None, spool_dir=None):
    """Run until stop or fault; recover committed batches on every start."""
    validate_production_config(cfg)
    stop_event = stop_event or threading.Event()
    guard_spool = DurableSpool(Path(spool_dir or cfg.SPOOL_DIR), cfg.SPOOL_MAX_BYTES)
    try:
        prior_destination = guard_spool.state_value("destination")
        prior_identity = guard_spool.state_value("destination_identity")
        current_identity = destination_identity(cfg)
        if prior_destination and (prior_destination != cfg.DESTINATION or (prior_identity and prior_identity != current_identity)):
            pending = guard_spool.pending_batches + len(guard_spool.pending_gaps())
            guard_spool.record_cutover(prior_destination, cfg.DESTINATION, pending)
        guard_spool.set_state("destination", cfg.DESTINATION)
        guard_spool.set_state("destination_identity", current_identity)
    finally:
        guard_spool.close()
    if destination is None:
        if cfg.DESTINATION == "mqtt":
            destination = MQTTProductionDestination(cfg)
        elif cfg.DESTINATION == "influxdb":
            destination = InfluxProductionDestination(cfg)
        else:
            destination = TimescaleProductionDestination(cfg)
    pipeline = ProductionPipeline(cfg, Path(spool_dir or cfg.SPOOL_DIR), destination)
    writer_stop = threading.Event()
    writer_deadline = [None]
    writer_error = deque(maxlen=10)
    section_rows = cfg.SECTION_LENGTH * len(pipeline.enabled)
    writer_batch_group = max(1, min(_WRITER_BATCH_GROUP, _WRITER_MAX_ROWS // section_rows))

    def writer():
        while not writer_stop.is_set() or (
            (pipeline.pending_batches or pipeline.spool.pending_gaps())
            and time.monotonic() < writer_deadline[0]
        ):
            try:
                if not pipeline.flush_batches(writer_batch_group):
                    writer_stop.wait(0.1)
            except Exception as exc:
                writer_error.append(str(exc))
                pipeline.last_writer_error = str(exc)
                pipeline.publish_status()
                if writer_stop.is_set():
                    break
                writer_stop.wait(0.5)

    thread = threading.Thread(target=writer, name="ProductionWriter", daemon=True)
    thread.start()
    device = None
    fault = None
    try:
        device = daq_factory(cfg)
        while not stop_event.is_set():
            raw, end_ns = device.read(cfg.USER_BUFFER_SIZE)
            if raw:
                pipeline.capture(raw, end_ns,
                                 hardware_start_ns=getattr(device, "start_time_ns", None)
                                 if pipeline.frame_index == 0 else None,
                                 timing_reliable=getattr(device, "last_read_timing_reliable", False))
    except Exception as exc:
        fault = str(exc)
        pipeline.last_fault = fault
        pipeline.state = "failed"
        pipeline.open_gap(fault)
        pipeline.publish_status()
        log.exception("Production DAQ stopped: %s", fault)
    finally:
        if device is not None:
            try:
                device.close()
            except Exception as exc:
                log.exception("DAQ release failed")
                fault = fault or f"daq_release_failed: {exc}"
        writer_deadline[0] = time.monotonic() + 30
        writer_stop.set()
        thread.join(timeout=35)
        if thread.is_alive():
            log.warning("Writer timed out; committed batches remain in spool")
        pending = pipeline.pending_batches
        if fault is None:
            pipeline.open_gap("requested_stop")
        log.info("Production stopped; pending_batches=%s spool_bytes=%s", pending, pipeline.usage_bytes)
        if not thread.is_alive():
            pipeline.close()
    if fault:
        raise AcquisitionFault(fault)
    return {"pending_batches": pending, "writer_errors": list(writer_error)}
