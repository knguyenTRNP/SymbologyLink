# Contributing

Contributions are welcome through focused issues and pull requests.

## Development setup

```console
python -m venv .venv
# Activate the virtual environment, then run:
python -m pip install -e ".[all,test]"
python -m unittest discover -s tests -v
```

Use the virtual environment's Python executable when the environment is not activated.

## Pull requests

- Keep changes scoped to one problem.
- Add or update tests for behavior changes.
- Preserve evidence, provenance, and deterministic output.
- Document public interfaces and configuration changes.
- Do not commit credentials, customer datasets, caches, generated results, or virtual environments.

## Provider integrations

Provider implementations must normalize results into the shared candidate model, use bounded timeouts, respect provider rate limits, avoid logging credentials, and expose failures without failing unrelated records.
