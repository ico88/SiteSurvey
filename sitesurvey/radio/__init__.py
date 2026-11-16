from __future__ import annotations

from .base import RadioDriver, RadioError


def make_driver(cfg: dict) -> RadioDriver:
    rc = cfg["radio"]
    name = rc["driver"]
    mode = rc.get("mode", "dmr")
    if name == "motorola":
        from .motorola import MotorolaDriver
        return MotorolaDriver(rc["motorola"], mode)
    if name == "hytera":
        from .hytera import HyteraDriver
        return HyteraDriver(rc["hytera"], mode)
    if name == "simulator":
        from .simulator import SimulatorDriver
        return SimulatorDriver(rc["simulator"], mode)
    raise ValueError(f"driver radio sconosciuto: {name}")


__all__ = ["RadioDriver", "RadioError", "make_driver"]
