# Privacy

NocturNation collects nothing. No account, no telemetry, no analytics, no cloud — the app never sends your data anywhere.

- **No microphone.** The Tildagon has no mic and the app never listens to audio. As a Lume it only *receives* light commands over ESP-NOW; as a Director it reads the badge's motion sensor (taps), not sound.
- **No personal data.** Nothing about you is collected, stored, or transmitted. No contacts, location, or identity access.
- **Local radio only.** All traffic is short-range ESP-NOW — colour/brightness/timing light commands plus a liveness heartbeat. Nothing reaches the internet; the app drops WiFi entirely while a session is running.
- **No tracking between badges.** Badges don't exchange identities; the Trust-On-First-Use lock keys on a Director's random session id, not anything personal.
- **Open source.** Every line is public at [github.com/ratcliffej/nocturnation-tildagon](https://github.com/ratcliffej/nocturnation-tildagon) — inspect it yourself.

In short: it's a light that listens for light, not for you.
