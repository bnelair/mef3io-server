# Contributing to mef3io-server

Thanks for your interest in improving mef3io-server. This is a gRPC server (and
Python client) for concurrent, channels-and-time access to MEF 3.0 files, with a
shared tile cache and parallel decode. Decode itself lives in
[mef3io](https://github.com/bnelair/mef3io); this project is the service layer on
top of it.

## Ways to help

- **Report a bug** or **request a feature** via
  [GitHub issues](https://github.com/bnelair/mef3io-server/issues).
- **Ask a question** in
  [Discussions](https://github.com/bnelair/mef3io-server/discussions).
- **Send a pull request** for fixes and features (see below).

## Development setup

Requires Python 3.10+ (3.12 recommended). Decode is provided by
[mef3io](https://github.com/bnelair/mef3io), installed automatically as a
dependency.

```bash
git clone https://github.com/bnelair/mef3io-server && cd mef3io-server
pip install -e ".[dev]"          # runtime + test/benchmark/build tooling
python -m pytest                 # the fast suite (slow/benchmark are opt-in)
```

A plain `pytest` run excludes the long-running `slow`, `benchmark`, and
`crossover` suites (configured via `addopts` in `pyproject.toml`). Opt into them
explicitly:

```bash
pytest -m slow          # long functional tests against a generated 1-hour file
pytest -m benchmark     # performance benchmarks (see BENCHMARKS.md)
```

To build the docs locally:

```bash
pip install -e ".[docs]"
mkdocs serve            # live preview at http://127.0.0.1:8000
```

## Pull requests

- Branch from `main`; open the PR against `main`.
- **Add or update tests.** New behavior needs coverage; the suite in `tests/`
  cross-checks server reads against a direct `MefReader`.
- Run the suite (`pytest`) and keep it green. If you touch the decode/prefetch
  paths, also sanity-check `pytest -m slow`.
- **Close reader/writer sessions explicitly.** mef3io sessions must be closed
  (`reader.close()` / `writer.close()`) to release file handles — do not rely on
  GC. Leaked sessions surface as `nanobind: leaked instance` warnings at exit.
- Match the surrounding style; the code uses Google-style docstrings (rendered by
  mkdocstrings).
- CI must pass.

## Reporting good bugs

The most useful reports include the mef3io-server version, the
[mef3io](https://github.com/bnelair/mef3io) version (`mef3io.__version__`), OS and
Python version, a minimal snippet, and the full error/traceback. If a specific
session triggers it, note whether it also reproduces with a direct `mef3io`
read (to localize the issue to the server vs. the decode layer). Never attach
clinical or subject-identifying data.

## Reporting security issues

Please follow [`SECURITY.md`](SECURITY.md) rather than opening a public issue.

## License

By contributing you agree that your contributions are licensed under the
project's [Apache 2.0 license](LICENSE).
