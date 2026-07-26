"""Image helpers for vision captcha (stdlib + optional pillow)."""
from __future__ import annotations

import base64
import io
from typing import Any


def b64_from_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def screenshot_element_b64(locator) -> str:
    raw = locator.screenshot(type="png")
    return b64_from_bytes(raw)


def screenshot_page_b64(page) -> str:
    raw = page.screenshot(type="png", full_page=False)
    return b64_from_bytes(raw)


def annotate_grid_labels(image_b64: str, cols: int, rows: int) -> str:
    """Draw #0.. labels on a grid image when Pillow is available; else passthrough."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return image_b64
    raw = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    cell_w = w / max(1, cols)
    cell_h = h / max(1, rows)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    idx = 0
    for r in range(rows):
        for c in range(cols):
            x = int(c * cell_w + 4)
            y = int(r * cell_h + 4)
            draw.text((x, y), f"#{idx}", fill=(255, 0, 0), font=font)
            idx += 1
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return b64_from_bytes(buf.getvalue())


def canvas_cell_center(
    box: dict[str, Any],
    index: int,
    *,
    rows: int,
    cols: int,
    top_frac: float,
    bottom_frac: float,
    left_frac: float,
    right_frac: float,
) -> tuple[float, float]:
    """Map tile index to absolute page coordinates inside an element bounding box."""
    x = float(box.get("x") or 0)
    y = float(box.get("y") or 0)
    w = float(box.get("width") or 0)
    h = float(box.get("height") or 0)
    inner_x0 = x + w * left_frac
    inner_y0 = y + h * top_frac
    inner_w = w * (1.0 - left_frac - right_frac)
    inner_h = h * (1.0 - top_frac - bottom_frac)
    r = index // max(1, cols)
    c = index % max(1, cols)
    cx = inner_x0 + (c + 0.5) * (inner_w / max(1, cols))
    cy = inner_y0 + (r + 0.5) * (inner_h / max(1, rows))
    return cx, cy
