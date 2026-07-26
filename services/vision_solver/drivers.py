"""Sync Playwright drivers for visual captcha modes."""
from __future__ import annotations

import time
from typing import Callable

from .imaging import annotate_grid_labels, canvas_cell_center, screenshot_element_b64, screenshot_page_b64
from .schema import CaptchaSpec, default_prompt_for
from .vision import vote_answer

InterruptChecker = Callable[[], None] | None


def _sleep_ms(ms: int, interrupt_checker: InterruptChecker = None) -> None:
    end = time.monotonic() + max(0, int(ms)) / 1000.0
    while time.monotonic() < end:
        if interrupt_checker:
            interrupt_checker()
        time.sleep(min(0.2, max(0.0, end - time.monotonic())))


def find_challenge_frame(page, frame_match: list[str]):
    if not frame_match:
        return page
    needles = [str(x).lower() for x in frame_match if x]
    for fr in page.frames:
        url = (fr.url or "").lower()
        if any(n in url for n in needles):
            return fr
    return page


def read_instruction(frame, sel: str) -> str:
    if not sel:
        return "select matching images"
    for part in sel.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            loc = frame.locator(part).first
            if loc.count() > 0:
                text = (loc.inner_text(timeout=1000) or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return "select matching images"


def challenge_still_present(page, spec: CaptchaSpec) -> bool:
    if spec.success_markers:
        try:
            body = (page.locator("body").inner_text(timeout=1000) or "").lower()
            if any(m.lower() in body for m in spec.success_markers):
                return False
        except Exception:
            pass
    if spec.success_gone_frame and spec.frame_match:
        fr = find_challenge_frame(page, spec.frame_match)
        if fr is page:
            # no matching frame left
            return False
        try:
            # frame may be detached
            _ = fr.url
            return True
        except Exception:
            return False
    return True


def solve_canvas_grid(page, spec: CaptchaSpec, *, interrupt_checker: InterruptChecker = None) -> bool:
    frame = find_challenge_frame(page, spec.frame_match)
    instruction = read_instruction(frame, spec.challenge_text_sel)
    prompt_t = (spec.prompt or default_prompt_for("canvas_grid")).replace("{instruction}", instruction)
    for _ in range(max(1, spec.max_rounds)):
        if interrupt_checker:
            interrupt_checker()
        try:
            canvas = frame.locator(spec.canvas_sel or "canvas").first
            if canvas.count() == 0:
                return not challenge_still_present(page, spec)
            img = screenshot_element_b64(canvas)
            img = annotate_grid_labels(img, spec.cols, spec.rows)
            result = vote_answer(
                prompt_t,
                img,
                n_options=spec.rows * spec.cols,
                rounds=3,
                answer_format="PICK_LIST",
                timeout_seconds=float(spec.deadline or 55),
                interrupt_checker=interrupt_checker,
            )
            picks = result.get("answer") or []
            if isinstance(picks, int):
                picks = [picks]
            box = canvas.bounding_box()
            if box:
                for idx in picks:
                    try:
                        cx, cy = canvas_cell_center(
                            box,
                            int(idx),
                            rows=spec.rows,
                            cols=spec.cols,
                            top_frac=spec.grid_top_frac,
                            bottom_frac=spec.grid_bottom_frac,
                            left_frac=spec.grid_left_frac,
                            right_frac=spec.grid_right_frac,
                        )
                        page.mouse.click(cx, cy)
                        _sleep_ms(200, interrupt_checker)
                    except Exception:
                        continue
            if spec.submit_sel:
                try:
                    frame.locator(spec.submit_sel).first.click(timeout=2000)
                except Exception:
                    pass
            _sleep_ms(spec.settle_ms, interrupt_checker)
            if not challenge_still_present(page, spec):
                return True
        except Exception:
            _sleep_ms(spec.settle_ms, interrupt_checker)
    return not challenge_still_present(page, spec)


def solve_grid_select(page, spec: CaptchaSpec, *, interrupt_checker: InterruptChecker = None) -> bool:
    frame = find_challenge_frame(page, spec.frame_match)
    instruction = read_instruction(frame, spec.challenge_text_sel)
    prompt_t = (spec.prompt or default_prompt_for("grid_select")).replace("{instruction}", instruction)
    for _ in range(max(1, spec.max_rounds)):
        if interrupt_checker:
            interrupt_checker()
        try:
            if spec.grid_image_sel:
                img = screenshot_element_b64(frame.locator(spec.grid_image_sel).first)
            else:
                img = screenshot_page_b64(frame if hasattr(frame, "screenshot") else page)
            img = annotate_grid_labels(img, spec.cols, spec.rows)
            result = vote_answer(
                prompt_t,
                img,
                answer_format="PICK_LIST",
                timeout_seconds=float(spec.deadline or 55),
                interrupt_checker=interrupt_checker,
            )
            picks = result.get("answer") or []
            tiles = frame.locator(spec.tile_sel) if spec.tile_sel else None
            if tiles is not None:
                for idx in picks:
                    try:
                        tiles.nth(int(idx)).click(timeout=1500)
                    except Exception:
                        continue
            if spec.submit_sel:
                try:
                    frame.locator(spec.submit_sel).first.click(timeout=2000)
                except Exception:
                    pass
            _sleep_ms(spec.settle_ms, interrupt_checker)
            if not challenge_still_present(page, spec):
                return True
        except Exception:
            _sleep_ms(spec.settle_ms, interrupt_checker)
    return not challenge_still_present(page, spec)


def solve_single_pick(page, spec: CaptchaSpec, *, interrupt_checker: InterruptChecker = None) -> bool:
    """Generic single-pick: screenshot + vote index + click candidate / next/submit roles."""
    frame = find_challenge_frame(page, spec.frame_match)
    instruction = read_instruction(frame, spec.challenge_text_sel)
    prompt_t = (spec.prompt or default_prompt_for("single_pick")).replace("{instruction}", instruction)
    prompt_t = prompt_t.replace("{last}", "8")
    for _ in range(max(1, spec.max_rounds)):
        if interrupt_checker:
            interrupt_checker()
        try:
            img = screenshot_page_b64(page)
            result = vote_answer(
                prompt_t,
                img,
                n_options=12,
                answer_format="ANSWER_INDEX",
                timeout_seconds=float(spec.deadline or 55),
                interrupt_checker=interrupt_checker,
            )
            idx = result.get("answer")
            if idx is not None and spec.cand_sel:
                try:
                    # navigate next buttons to reach index
                    for _i in range(int(idx)):
                        if spec.next_btn_role:
                            frame.get_by_role("button", name=spec.next_btn_role).click(timeout=1500)
                        _sleep_ms(200, interrupt_checker)
                except Exception:
                    pass
            if spec.submit_role:
                try:
                    frame.get_by_role("button", name=spec.submit_role).click(timeout=2000)
                except Exception:
                    pass
            elif spec.submit_sel:
                try:
                    frame.locator(spec.submit_sel).first.click(timeout=2000)
                except Exception:
                    pass
            _sleep_ms(spec.settle_ms, interrupt_checker)
            if not challenge_still_present(page, spec):
                return True
        except Exception:
            _sleep_ms(spec.settle_ms, interrupt_checker)
    return not challenge_still_present(page, spec)


def solve_canvas_drag(page, spec: CaptchaSpec, *, interrupt_checker: InterruptChecker = None) -> bool:
    # Minimal: treat as canvas_grid vote then click — full drag needs FROM/TO parse.
    return solve_canvas_grid(page, spec, interrupt_checker=interrupt_checker)
