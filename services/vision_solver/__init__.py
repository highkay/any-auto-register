"""Clean-room vision captcha helpers (sync Playwright + HTTP vision vote)."""

from .schema import CaptchaSpec
from .solver import solve_on_page, VisionSolver
from .github_puzzle import solve_github_arkose_puzzle

__all__ = [
    "CaptchaSpec",
    "VisionSolver",
    "solve_on_page",
    "solve_github_arkose_puzzle",
]
