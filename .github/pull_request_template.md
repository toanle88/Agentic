## Summary

## Validation

- [ ] `python -m unittest discover -s tests -q`
- [ ] `python -m py_compile agentic.py agentic_dashboard.py agentic_shell.py src/agentic_runner/*.py`
- [ ] `git diff --check`

## Safety review

- [ ] No credentials, private keys, or production connection strings added.
- [ ] Developer/reviewer separation is preserved.
- [ ] Commands remain argv arrays and do not use shell interpolation.
- [ ] User-visible configuration or CLI behavior is documented.
