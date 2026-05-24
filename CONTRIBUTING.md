# Contributing to NocturNation (Tildagon app)

Thanks for your interest. Issues and pull requests are welcome.

## Ground rules

- Be kind and constructive.
- Contributions are accepted under the repository's [MIT licence](LICENSE).
- For anything that touches the wire protocol, the [protocol manual](https://github.com/ratcliffej/nocturnation-docs/blob/main/manuals/protocol-manual.md) is the source of truth. A wire change must update the manual **and** both implementations - this app and the [M5 firmware](https://github.com/ratcliffej/nocturnation-stickc) - or it will not be merged.

## Pull requests

- Keep PRs focused: one logical change per PR.
- Explain *why*, not just *what*.
- **Every PR is read line-by-line before merge.** Expect heightened scrutiny for changes to:
  - `.github/` (workflows, actions, repository configuration),
  - dependencies and packaging (`pyproject.toml`, `tildagon.toml`, vendored files such as `uQR.py`),
  - `deploy.sh` and anything on the release-to-badge path (see [RELEASING.md](RELEASING.md)),
  - the protocol, receive, and TOFU code under `apps/nocturnation/nocturnation/`.
- New or changed behaviour needs tests. PRs that do not pass `pytest` will not be merged.

## Testing

```sh
pip install -e ".[dev]"
pytest
```

The suite runs on host Python - no badge required. Byte-level frame behaviour is parity-tested against the protocol manual's reference vectors; keep those in sync with the manual.

## Adding a Show

A Show is a folder under `apps/nocturnation/shows/<id>/` exposing `make_show()`, auto-discovered at boot. The authoring model mirrors the M5 firmware; see the [developer guide](https://github.com/ratcliffej/nocturnation-docs/blob/main/developing-shows.md) for hooks, properties, drawing, and the host-test pattern.
