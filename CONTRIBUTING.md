# Contributing to ccswitch

Thanks for taking a look. ccswitch is a small, focused tool, and contributions
are welcome.

## Reporting issues

Open an issue describing what you expected, what actually happened, your
operating system, and your Python version (`python --version`). Please never
paste credentials or the contents of your `~/.cc-accounts` folder.

## Development setup

```
git clone https://github.com/GG-Santos/ccswitch.git
cd ccswitch
pip install -e ".[test]"
pytest
```

The test suite runs against throwaway folders and mocks all network calls, so it
never touches your real accounts.

## Pull requests

- Keep changes focused, and add a test where it makes sense.
- Run `pytest` before opening the pull request.
- Keep the code style consistent with what is already there.
