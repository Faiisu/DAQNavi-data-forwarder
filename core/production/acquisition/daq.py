"""Advantech waveform DAQ hardware adapter."""

from __future__ import annotations

import math
import re
import time
from ..fault import AcquisitionFault

class AdvantechDaq:
    """Small hardware adapter. getDataF64 count is interleaved scalar values."""

    def __init__(self, cfg):
        from Automation.BDaq.WaveformAiCtrl import WaveformAiCtrl
        from Automation.BDaq.BDaqApi import BioFailed
        self._bio_failed = BioFailed
        self.cfg = cfg
        self.device = WaveformAiCtrl(cfg.DEVICE_DESCRIPTION)
        if cfg.PROFILE_PATH:
            self.device.loadProfile = cfg.PROFILE_PATH
        self.device.conversion.channelStart = cfg.START_CHANNEL
        self.device.conversion.channelCount = cfg.CHANNEL_COUNT
        self.device.conversion.clockRate = cfg.CLOCK_RATE
        self.device.record.sectionCount = cfg.SECTION_COUNT
        self.device.record.sectionLength = cfg.SECTION_LENGTH
        for index in range(cfg.START_CHANNEL, cfg.START_CHANNEL + cfg.CHANNEL_COUNT):
            ch = cfg.channels[index]
            if (re.match(r'^PCI-1716(?:H|L)?(?:,|$)', cfg.DEVICE_DESCRIPTION, re.IGNORECASE)
                    and index % 2 and not ch.enabled
                    and cfg.channels[index - 1].signal_type_str == 'Differential'):
                # The card configures the odd input as the differential negative leg.
                continue
            self.device.channels[index].signalType = ch.signal_type
            self.device.channels[index].valueRange = ch.value_range
        if BioFailed(self.device.prepare()):
            self.close()
            raise AcquisitionFault("daq_start_failed")
        before_start_ns = time.time_ns()
        start_result = self.device.start()
        after_start_ns = time.time_ns()
        if BioFailed(start_result):
            self.close()
            raise AcquisitionFault("daq_start_failed")
        self.start_time_ns = (before_start_ns + after_start_ns) // 2
        self.empty_reads = 0
        self.last_read_timing_reliable = False

    def read(self, count):
        # The SDK accepts milliseconds. A finite timeout lets SIGTERM finish
        # even if the card stops producing samples; timeout may return data.
        before_read_ns = time.monotonic_ns()
        result, returned, data = self.device.getDataF64(count, 1000)[:3]
        read_duration_ns = time.monotonic_ns() - before_read_ns
        end_ns = time.time_ns()
        if self._bio_failed(result):
            raise AcquisitionFault("daq_read_failed")
        status = getattr(result, "name", str(result))
        if status not in ("Success", "WarningFuncTimeout"):
            raise AcquisitionFault("daq_cache_overflow" if status == "WarningCacheOverflow"
                                   else f"daq_warning_{status}")
        if returned < 0 or returned > count:
            raise AcquisitionFault("invalid_daq_count")
        if returned % self.cfg.CHANNEL_COUNT:
            raise AcquisitionFault("unaligned_daq_batch")
        expected_read_ns = returned // self.cfg.CHANNEL_COUNT * 1_000_000_000 // self.cfg.CLOCK_RATE
        self.last_read_timing_reliable = (
            status == "Success" and returned == count
            and read_duration_ns >= expected_read_ns // 2
        )
        self.empty_reads = 0 if returned else self.empty_reads + 1
        # getDataF64 can return an empty timeout while the SDK is filling a
        # section. Allow its expected collection time plus two polling cycles.
        max_empty_reads = max(3, math.ceil(self.cfg.SECTION_LENGTH / self.cfg.CLOCK_RATE) + 2)
        if self.empty_reads >= max_empty_reads:
            raise AcquisitionFault("daq_stalled")
        return list(data[:returned]), end_ns

    def close(self):
        if getattr(self, "device", None):
            try:
                self.device.stop()
            finally:
                self.device.dispose()
