"""Sync entry: solve_on_page(page, spec) -> bool."""
from __future__ import annotations

from typing import Callable

from .drivers import solve_canvas_drag, solve_canvas_grid, solve_grid_select, solve_single_pick
from .schema import CaptchaSpec, load_preset

InterruptChecker = Callable[[], None] | None


def _coerce_spec(spec) -> CaptchaSpec:
    if isinstance(spec, CaptchaSpec):
        return spec
    if isinstance(spec, str):
        # preset name or json path
        if spec.endswith(".json"):
            return CaptchaSpec.from_json(spec)
        return load_preset(spec)
    if isinstance(spec, dict):
        return CaptchaSpec.from_dict(spec)
    raise TypeError(f"unsupported captcha spec: {type(spec)}")


def solve_on_page(page, spec, *, shot_dir=None, interrupt_checker: InterruptChecker = None) -> bool:
    """Drive a visual captcha on *page* according to *spec* (sync)."""
    spec_obj = _coerce_spec(spec)
    mode = (spec_obj.mode or "grid_select").strip().lower()
    if mode == "grid_select":
        return solve_grid_select(page, spec_obj, interrupt_checker=interrupt_checker)
    if mode == "single_pick":
        return solve_single_pick(page, spec_obj, interrupt_checker=interrupt_checker)
    if mode == "canvas_grid":
        return solve_canvas_grid(page, spec_obj, interrupt_checker=interrupt_checker)
    if mode == "canvas_drag":
        return solve_canvas_drag(page, spec_obj, interrupt_checker=interrupt_checker)
    raise ValueError(f"unknown captcha mode: {mode}")


class VisionSolver:
    def __init__(self, spec, shot_dir: str | None = None):
        self.spec = _coerce_spec(spec)
        self.shot_dir = shot_dir

    def solve(self, page, *, interrupt_checker: InterruptChecker = None) -> bool:
        return solve_on_page(
            page,
            self.spec,
            shot_dir=self.shot_dir,
            interrupt_checker=interrupt_checker,
        )
