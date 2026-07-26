"""Clean-room human-like mouse helpers (sync Playwright).

WindMouse-style trajectory + Ornstein-Uhlenbeck tremor for press-and-hold.
Independent reimplementation of publicly described algorithms; not a copy of
third-party source trees. See docs/THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import math
import os
import random
import time
from typing import Callable

InterruptChecker = Callable[[], None] | None

_WM_GRAVITY = 9.0
_WM_WIND = 3.0
_WM_MAX_STEP = 14.0
_WM_TARGET_AREA = 12.0


def _debug(msg: str) -> None:
    if str(os.environ.get("HUMAN_MOUSE_DEBUG", "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print(f"  [human_mouse] {msg}", flush=True)


def _tremor_px() -> float:
    try:
        return max(0.3, float(os.environ.get("HUMAN_MOUSE_TREMOR_PX", "1.6")))
    except Exception:
        return 1.6


def _checkpoint(checker: InterruptChecker) -> None:
    if checker is not None:
        checker()


def _sleep(seconds: float, interrupt_checker: InterruptChecker = None) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        _checkpoint(interrupt_checker)
        chunk = min(0.05, remaining)
        time.sleep(chunk)
        remaining -= chunk


def windmouse_path(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    gravity: float = _WM_GRAVITY,
    wind: float = _WM_WIND,
    max_step: float = _WM_MAX_STEP,
    target_area: float = _WM_TARGET_AREA,
) -> list[tuple[float, float]]:
    """Return float points from (x0,y0) to (x1,y1) with gravity + wind motion."""
    points: list[tuple[float, float]] = []
    cx, cy = float(x0), float(y0)
    vx = vy = 0.0
    wx = wy = 0.0
    sqrt3 = math.sqrt(3.0)
    sqrt5 = math.sqrt(5.0)
    dist = math.hypot(x1 - cx, y1 - cy)
    if dist < 1.0:
        return [(float(x1), float(y1))]

    step_limit = float(max_step)
    guard = 0
    while dist >= 1.0 and guard < 10000:
        guard += 1
        w = min(wind, dist)
        if dist >= target_area:
            wx = wx / sqrt3 + (2.0 * random.random() - 1.0) * w / sqrt5
            wy = wy / sqrt3 + (2.0 * random.random() - 1.0) * w / sqrt5
        else:
            wx /= sqrt3
            wy /= sqrt3
            if step_limit < 3:
                step_limit = random.random() * 3.0 + 3.0
            else:
                step_limit /= sqrt5

        vx += wx + gravity * (x1 - cx) / dist
        vy += wy + gravity * (y1 - cy) / dist
        v_mag = math.hypot(vx, vy)
        if v_mag > step_limit:
            v_clip = step_limit / 2.0 + random.random() * step_limit / 2.0
            vx = (vx / v_mag) * v_clip
            vy = (vy / v_mag) * v_clip
        cx += vx
        cy += vy
        dist = math.hypot(x1 - cx, y1 - cy)
        points.append((cx, cy))

    points.append((float(x1), float(y1)))
    return points


def tremor_offsets(
    n: int,
    dt: float = 0.05,
    theta: float = 6.0,
    sigma: float | None = None,
    clamp: float | None = None,
    seed: int | None = None,
) -> list[tuple[float, float]]:
    """Ornstein-Uhlenbeck tremor offsets with soft clamp."""
    rng = random.Random(seed) if seed is not None else random
    if clamp is None:
        clamp = _tremor_px()
    if sigma is None:
        sigma = clamp * 9.0
    out: list[tuple[float, float]] = []
    x = y = 0.0
    vx = vy = 0.0
    sdt = math.sqrt(dt)
    for _ in range(max(1, int(n))):
        vx += -theta * vx * dt + sigma * sdt * rng.gauss(0.0, 1.0)
        vy += -theta * vy * dt + sigma * sdt * rng.gauss(0.0, 1.0)
        x += vx * dt
        y += vy * dt
        if x > clamp or x < -clamp:
            x = max(-clamp, min(clamp, x))
            vx *= -0.4
        if y > clamp or y < -clamp:
            y = max(-clamp, min(clamp, y))
            vy *= -0.4
        out.append((x, y))
    return out


def human_move_to(
    page,
    x: float,
    y: float,
    start: tuple[float, float] | None = None,
    *,
    interrupt_checker: InterruptChecker = None,
) -> list[tuple[float, float]]:
    """Move mouse along WindMouse path (sync Playwright page.mouse)."""
    if start is None:
        sx = x + random.uniform(-260, 260)
        sy = y + random.uniform(-180, 180)
        page.mouse.move(sx, sy)
        _sleep(random.uniform(0.04, 0.12), interrupt_checker)
    else:
        sx, sy = start
    path = windmouse_path(sx, sy, x, y)
    n = len(path)
    for i, (px, py) in enumerate(path):
        _checkpoint(interrupt_checker)
        page.mouse.move(px + random.uniform(-0.6, 0.6), py + random.uniform(-0.6, 0.6))
        frac = i / max(1, n - 1)
        bell = math.sin(math.pi * frac)
        delay = random.uniform(0.004, 0.010) + (1.0 - bell) * random.uniform(0.004, 0.018)
        _sleep(delay, interrupt_checker)
    _debug(f"move_to ({x:.0f},{y:.0f}) via {n} pts")
    return path


def human_press_and_hold(
    page,
    cx: float,
    cy: float,
    is_done: Callable[[], bool] | None = None,
    max_hold: float = 14.0,
    min_hold: float = 1.5,
    check_interval: float = 0.5,
    start: tuple[float, float] | None = None,
    *,
    interrupt_checker: InterruptChecker = None,
) -> tuple[float, bool]:
    """Press-and-hold with tremor; returns (held_seconds, passed)."""
    human_move_to(page, cx, cy, start=start, interrupt_checker=interrupt_checker)
    _sleep(random.uniform(0.12, 0.35), interrupt_checker)
    page.mouse.down()

    t0 = time.monotonic()
    tick = random.uniform(0.03, 0.07)
    passed = False
    last_check = 0.0
    tre = tremor_offsets(int(max_hold / tick) + 32, dt=tick)
    ti = 0

    while True:
        _checkpoint(interrupt_checker)
        elapsed = time.monotonic() - t0
        if elapsed >= max_hold:
            break
        if ti >= len(tre):
            tre = tremor_offsets(64, dt=tick)
            ti = 0
        dx, dy = tre[ti]
        ti += 1
        page.mouse.move(cx + dx, cy + dy)

        if is_done is not None and elapsed > min_hold and (elapsed - last_check) > check_interval:
            last_check = elapsed
            try:
                if is_done():
                    passed = True
                    _sleep(random.uniform(0.12, 0.36), interrupt_checker)
                    break
            except Exception:
                pass
        _sleep(random.uniform(tick * 0.6, tick * 1.4), interrupt_checker)

    _sleep(random.uniform(0.03, 0.12), interrupt_checker)
    page.mouse.up()
    held = time.monotonic() - t0
    _debug(f"hold {held:.1f}s passed={passed}")
    return held, passed
