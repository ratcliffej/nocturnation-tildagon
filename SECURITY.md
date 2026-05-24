# Security policy

This is the NocturNation app for the EMF Tildagon badge - an open-source, hobbyist project maintained by a single developer. It is not a certified medical, broadcast, or safety-critical device (see the [architecture spec's safety section](https://github.com/ratcliffej/nocturnation-docs/blob/main/architecture.md#15-safety-considerations)). The measures here are best-effort.

People flash published builds of this app onto their own badges, so the **release-to-badge path is the channel we take most seriously**. How a release is produced and how you can verify a build matches its source is documented in [RELEASING.md](RELEASING.md).

## Reporting a vulnerability

Please report security issues **privately - do not open a public issue**.

Use GitHub's **"Report a vulnerability"** button under this repository's **Security** tab (Security → Advisories). That opens a private advisory visible only to the maintainer.

Expect a best-effort acknowledgement. There is no formal SLA - this is a single-maintainer hobby project. Coordinated disclosure is appreciated: please give us a chance to fix an issue before disclosing it publicly.

## Scope

**In scope:**

- The app code in this repository (`apps/nocturnation/` - protocol, receive pipeline, render, Shows, director).
- ESP-NOW frame parsing/encoding and the Trust-On-First-Use source locking.
- The release and deploy path (`tildagon.toml`, `deploy.sh`, the published release artefacts).

**Out of scope:**

- The Tildagon OS / `badge-2024-software` and EMF badge infrastructure - report those to [EMF](https://github.com/emfcamp).
- Dev/test dependencies declared in `pyproject.toml` - report to their own upstreams.
- **By-design properties of the open protocol.** NocturNation broadcasts *unauthenticated* ESP-NOW frames; anyone in radio range can transmit. A "spoofed show on an open channel" is a documented design trade-off, not a vulnerability; see the [protocol manual](https://github.com/ratcliffej/nocturnation-docs/blob/main/manuals/protocol-manual.md) and [architecture spec §16](https://github.com/ratcliffej/nocturnation-docs/blob/main/architecture.md#16-security-model-overview). The badge transmits on the hobby channel (1) only and applies TOFU + Performance-band filtering on receive; these are best-effort, not cryptographic.

## Supported versions

The latest tagged release is the supported line; fixes ship as a new release (see [RELEASING.md](RELEASING.md)).
