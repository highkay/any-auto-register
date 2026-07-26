"""CaptchaSpec — declarative description of a visual captcha challenge."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List


@dataclass
class CaptchaSpec:
    name: str = "generic"
    frame_match: List[str] = field(default_factory=list)
    mode: str = "grid_select"  # grid_select | single_pick | canvas_grid | canvas_drag
    prompt: str = ""
    challenge_text_sel: str = ""
    tile_sel: str = ""
    grid_image_sel: str = ""
    submit_sel: str = ""
    ref_sel: str = ""
    cand_sel: str = ""
    next_btn_role: str = ""
    prev_btn_role: str = ""
    submit_role: str = ""
    canvas_sel: str = "canvas"
    rows: int = 3
    cols: int = 3
    grid_top_frac: float = 0.30
    grid_bottom_frac: float = 0.036
    grid_left_frac: float = 0.164
    grid_right_frac: float = 0.164
    grid_pad_frac: float = 0.0
    example_text_sel: str = ""
    answer_format: str = "PICK_LIST"  # PICK_LIST | ANSWER_INDEX
    max_rounds: int = 6
    deadline: int = 55
    answer_max_tokens: int = 900
    settle_ms: int = 800
    success_gone_frame: bool = True
    success_markers: List[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "CaptchaSpec":
        valid = {f for f in CaptchaSpec.__dataclass_fields__}
        return CaptchaSpec(**{k: v for k, v in (d or {}).items() if k in valid})

    @staticmethod
    def from_json(path: str | Path) -> "CaptchaSpec":
        with open(path, encoding="utf-8") as f:
            return CaptchaSpec.from_dict(json.load(f))

    def to_dict(self) -> dict:
        return asdict(self)


GRID_PROMPT = (
    "You are an accessibility helper reading an image-selection challenge. "
    "The image below is a grid of tiles labeled #0,#1,#2,... in reading order. "
    'Task: "{instruction}". '
    "Return exactly one line: PICK=[a,b,c] (JSON list of matching tile numbers; PICK=[] if none)."
)

SINGLE_PROMPT = (
    "You are an accessibility helper solving a visual matching puzzle. "
    "Top = REFERENCE. Below = candidates #0..#{last}. "
    'Task: "{instruction}". Pick ONE best match. '
    "Last line exactly: ANSWER=<number>."
)

DRAG_PROMPT = (
    "You are an accessibility helper solving a drag puzzle on a canvas. "
    'Task: "{instruction}". '
    "Return last line: FROM=<x>,<y> TO=<x>,<y> with normalized 0..1 coordinates."
)

CANVAS_GRID_PROMPT = (
    "You are an accessibility helper reading an hCaptcha-style canvas grid. "
    "Cells labeled #0,#1,... left-to-right top-to-bottom. "
    'Task: "{instruction}". '
    "Last line: PICK=[a,b] and optionally ANSWER=<best>."
)


def default_prompt_for(mode: str) -> str:
    return {
        "grid_select": GRID_PROMPT,
        "single_pick": SINGLE_PROMPT,
        "canvas_drag": DRAG_PROMPT,
        "canvas_grid": CANVAS_GRID_PROMPT,
    }.get(mode, GRID_PROMPT)


def preset_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "presets" / f"{name}.json"


def load_preset(name: str) -> CaptchaSpec:
    path = preset_path(name)
    if path.exists():
        return CaptchaSpec.from_json(path)
    # Built-in fallbacks
    if name == "hcaptcha":
        return CaptchaSpec(
            name="hcaptcha",
            frame_match=["frame=challenge", "hcaptcha"],
            mode="canvas_grid",
            challenge_text_sel="#prompt-question, .prompt-text",
            canvas_sel="canvas",
            submit_sel=".button-submit",
            rows=3,
            cols=3,
            answer_format="PICK_LIST",
            max_rounds=8,
            success_gone_frame=True,
        )
    if name == "arkose_match":
        return CaptchaSpec(
            name="arkose_match",
            frame_match=["arkose", "funcaptcha", "octocaptcha"],
            mode="single_pick",
            answer_format="ANSWER_INDEX",
            max_rounds=6,
        )
    return CaptchaSpec(name=name)
