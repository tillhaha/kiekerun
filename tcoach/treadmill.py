"""Treadmill abstraction + simulator + program runner.

The hardware layer (Bluetooth FTMS or a vendor protocol) will implement `Treadmill`.
Everything else - program execution, safety checks, recording - already works
against `SimTreadmill`, so swapping in real hardware later changes one class.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable

from .machine import has_errors, validate
from .models import Block, Workout
from .session import Sample


class SafetyError(RuntimeError):
    pass


class Treadmill(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def set_speed(self, kmh: float) -> None: ...

    @abstractmethod
    def set_incline(self, pct: float) -> None: ...

    @abstractmethod
    def read(self) -> tuple[float, float, int | None]:
        """Current (speed km/h, incline %, heart rate or None)."""


class SimTreadmill(Treadmill):
    """In-memory treadmill: reaches targets instantly. Optionally supplies a fake HR curve."""

    def __init__(self, hr_fn: Callable[[float, float], int] | None = None):
        self.speed = 0.0
        self.incline = 0.0
        self.connected = False
        self.commands: list[tuple[str, float]] = []
        self._hr_fn = hr_fn

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def set_speed(self, kmh: float) -> None:
        self.commands.append(("speed", kmh))
        self.speed = kmh

    def set_incline(self, pct: float) -> None:
        self.commands.append(("incline", pct))
        self.incline = pct

    def read(self):
        hr = self._hr_fn(self.speed, self.incline) if self._hr_fn else None
        return self.speed, self.incline, hr


class FakeClock:
    """Advances virtual time instantly (tests, simulation)."""

    def __init__(self):
        self.t = 0.0

    def tick(self) -> None:
        self.t += 1.0


class RealClock:
    """Waits until the next whole second (deadline-based, no drift)."""

    def __init__(self):
        self._next = time.monotonic()

    def tick(self) -> None:
        self._next += 1.0
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def run_workout(treadmill: Treadmill, workout: Workout, clock=None,
                on_block: Callable[[int, Block], None] | None = None) -> list[Sample]:
    """Execute a workout block by block and return 1 Hz samples read back from the treadmill.

    Refuses to start if the workout violates the machine limits (errors); warnings
    (mode switches, big jumps) are the caller's business.
    """
    issues = validate(workout)
    if has_errors(issues):
        raise SafetyError("; ".join(str(i) for i in issues if i.severity == "error"))
    clock = clock or RealClock()
    treadmill.connect()
    samples: list[Sample] = []
    t = 0
    try:
        for idx, block in enumerate(workout.flat(), start=1):
            treadmill.set_speed(block.speed_kmh)
            treadmill.set_incline(block.incline_pct)
            if on_block:
                on_block(idx, block)
            for _ in range(block.duration_s):
                clock.tick()
                speed, incline, hr = treadmill.read()
                samples.append(Sample(t, speed, incline, hr))
                t += 1
    finally:
        treadmill.set_speed(0.0)
        treadmill.disconnect()
    return samples
