# Scientesis project guidance

- Keep the app modular: `app.py` is presentation only; validation, persistence, RL execution, and critique belong in their existing `src/scientesis/` modules.
- Work one bounded feature at a time. Read the relevant module and tests, change only that area, and add or update focused tests before expanding scope.
- Preserve the human-authorization boundary: proposals are inert until explicitly approved, approval covers the exact configuration, and the brief is revalidated immediately before execution.
- Keep experiment limits and success/safety definitions in the ResearchBrief; do not silently invent or widen them.
- Treat actuator saturation as a simulation proxy, never as physical-robot safety evidence. Keep interpretations human-owned.
- Optional external services belong in isolated adapters. Never commit API keys, local databases, model checkpoints, or generated run artifacts.
- Read `docs/scientesis_design_spec.md` for product requirements and `README.md` for the current implementation boundary and setup.
- Connection settings use `services/integration_settings.py` and the in-app Settings tab. Read saved overrides with environment fallback; never mutate process environment variables. `data/integration-settings.json` is private, unencrypted local storage excluded from Git; do not put tokens in the research database or audit trail.
