# Samiad booking automation — notes for Claude Code

- Read README.md first, then the build spec if Alex shares it.
- Money rules live in `samiad/rules.py` (pure, fully tested). Change rules there and add a test
  in `tests/test_rules.py` with a real Samiad case. Run `python -m pytest` before every commit.
- All writes go through `Service.write()` in `samiad/flows.py` so shadow mode stays honest.
  Never call a client's write method directly from flow code.
- Never edit `templates/source/`; change templates via `scripts/prepare_templates.py`.
- Keys live in `.env` / Railway Variables only. Never print or commit them.
- HubSpot portal is EU (app-eu1). Pipeline ids are in rules.py; Closed Won stages are looked up.
- Alex prefers concise, plain-English explanations and pros/cons rather than instructions.
