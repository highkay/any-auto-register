"""GitHub Arkose visual puzzle driver (sync).

Delivers variant-aware multi-round voting. This is the GitHub-specific path;
generic arkose_match preset alone is NOT sufficient.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from .imaging import screenshot_page_b64
from .vision import vote_answer

InterruptChecker = Callable[[], None] | None

_VARIANT_SEQUENCE = ("sequence", "order", "arrange", "correct order")
_VARIANT_ROTATE = ("rotate", "orientation", "which way", "turned")
_VARIANT_CHARACTER = ("character", "animal", "which one is", "pick the")
_VARIANT_WIRES = ("wire", "connect", "path", "pipe")


def _checkpoint(c: InterruptChecker) -> None:
    if c:
        c()


def detect_variant(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in _VARIANT_WIRES):
        return "wires"
    if any(k in t for k in _VARIANT_ROTATE):
        return "rotate"
    if any(k in t for k in _VARIANT_SEQUENCE):
        return "sequence"
    if any(k in t for k in _VARIANT_CHARACTER):
        return "character"
    return "sequence"


def _prompt_for(variant: str, instruction: str, n: int = 6) -> str:
    base = (
        "You are solving an Arkose Labs visual puzzle (accessibility helper). "
        f'Instruction: "{instruction}". There are about {n} options. '
    )
    if variant == "rotate":
        return base + "Pick the image with the correct orientation. Last line: ANSWER=<0-based index>."
    if variant == "character":
        return base + "Pick the matching character/object. Last line: ANSWER=<0-based index>."
    if variant == "wires":
        return base + "Pick the tile that completes the wire/path. Last line: ANSWER=<0-based index>."
    return base + "Pick the tile that fits the correct sequence/order. Last line: ANSWER=<0-based index>."


def _arkose_frames(page):
    out = []
    for fr in page.frames:
        u = (fr.url or "").lower()
        if any(k in u for k in ("octocaptcha", "arkose", "funcaptcha")):
            out.append(fr)
    return out


def _challenge_visible(page) -> bool:
    try:
        if _arkose_frames(page):
            return True
        if page.locator("iframe[src*=octocaptcha], iframe[src*=arkose]").count() > 0:
            return True
    except Exception:
        pass
    return False


def _read_instruction(page) -> str:
    for fr in _arkose_frames(page):
        try:
            text = (fr.locator("body").inner_text(timeout=1500) or "").strip()
            if text:
                # first non-empty line-ish
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                return " ".join(lines[:3])[:240]
        except Exception:
            continue
    try:
        return (page.locator("body").inner_text(timeout=1000) or "")[:240]
    except Exception:
        return "solve the visual puzzle"


def _click_answer(page, index: int) -> bool:
    """Best-effort click the Nth selectable tile inside arkose frames."""
    for fr in _arkose_frames(page):
        try:
            # Common: buttons / images in game
            candidates = fr.locator("button, [role=button], a, img")
            count = candidates.count()
            if count <= 0:
                continue
            target = candidates.nth(min(max(0, int(index)), count - 1))
            target.click(timeout=2000)
            return True
        except Exception:
            continue
    return False


def solve_github_arkose_puzzle(
    page,
    *,
    shot_dir: str | None = None,
    interrupt_checker: InterruptChecker = None,
    skip_variants: tuple[str, ...] = ("character",),
    max_rounds: int = 8,
) -> bool | str:
    """Solve GitHub Arkose visual puzzle rounds.

    Returns:
      True on success, False on failure, or \"SKIP_VARIANT\" when first round
      is a hard variant listed in skip_variants.
    """
    if not _challenge_visible(page):
        return True

    for round_i in range(max(1, int(max_rounds))):
        _checkpoint(interrupt_checker)
        if not _challenge_visible(page):
            return True
        instruction = _read_instruction(page)
        variant = detect_variant(instruction)
        if round_i == 0 and variant in set(skip_variants or ()):
            return "SKIP_VARIANT"

        prompt = _prompt_for(variant, instruction)
        try:
            img = screenshot_page_b64(page)
        except Exception:
            time.sleep(0.5)
            continue
        result = vote_answer(
            prompt,
            img,
            n_options=8,
            rounds=3,
            answer_format="ANSWER_INDEX",
            timeout_seconds=50,
            interrupt_checker=interrupt_checker,
        )
        idx = result.get("answer")
        if idx is None:
            # try click Visual puzzle / Submit fallbacks
            try:
                page.get_by_text("Visual puzzle", exact=False).first.click(timeout=1500)
            except Exception:
                pass
            time.sleep(1.0)
            continue
        _click_answer(page, int(idx))
        # try submit
        for fr in _arkose_frames(page):
            try:
                fr.get_by_text(re.compile(r"submit|verify|next", re.I)).first.click(timeout=1500)
            except Exception:
                pass
        time.sleep(1.2)
        if not _challenge_visible(page):
            return True

    return not _challenge_visible(page)
