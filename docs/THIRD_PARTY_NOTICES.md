# Third-Party Notices — Captcha / Vision Integration

## Decision (2026-07-26)

For integrating registration/captcha capabilities inspired by
[reg-factory](https://github.com/highkay/reg-factory):

| Component | Policy |
|-----------|--------|
| `services/vision_solver` (planned) | **Clean-room reimplementation**. Do **not** copy source from reg-factory `vision_solver/` or `common/agent_captcha.py`. |
| `core/human_mouse` (planned) | **Clean-room** WindMouse-style path generation from public literature; do **not** vendor reg-factory `common/human_mouse.py` (which notes spirit from LoseNine/ruyipage). |
| FunCaptcha / PerimeterX HTTP clients | Original implementation against public CapSolver / YesCaptcha / EZ-Captcha API shapes; reference only for task type names and field conventions. |
| BitBrowser / AdsPower / Clash Verge | **Not integrated** (hard dependency rejected). |

## Provenance notes

- reg-factory root has no formal LICENSE file; README describes educational use only.
- Copying large source trees would create derivative-work risk under unclear licensing.
- User decision (2026-07-26): prefer clean-room over NOTICE+vendoring of reg-factory sources.

## Gate

Code that implements vision page drivers or human mouse trajectories must not land until
this decision is followed. Token-only captcha providers (YesCaptcha FunCaptcha, CapSolver,
EZCaptcha) in `core/base_captcha.py` are first-party wrappers around third-party HTTP APIs
and are out of scope for this gate.

## References

- Design: `docs/superpowers/specs/2026-07-26-reg-factory-integration-design.md`
- Public WindMouse / human-like pointer literature (independent reimplementation only)
