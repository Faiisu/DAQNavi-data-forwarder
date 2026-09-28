"""Shared failure type for physical acquisition and durable storage."""


class AcquisitionFault(RuntimeError):
    """Physical acquisition cannot preserve or trust further measurements."""
