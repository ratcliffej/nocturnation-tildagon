# Releasing & build integrity

Attendees flash published builds of this app onto their own badges, so the release path is the one place where a compromised artefact could actually reach a user's device. This document covers how a release is made and how anyone can verify that what they flashed matches the published source.

## What a release is

The app ships as **plain, unminified MicroPython source** - `app.py` plus the `nocturnation/` and `shows/` packages at the repo root (the app-store installer requires `app.py` at the top of the tarball). There is **no build or compile step** - the files in a tagged release are byte-for-byte the files that run on the badge. That makes the build trivially reproducible: *the source is the artefact*. There is nothing for a build server to get wrong or for a supply-chain step to tamper with.

## Cutting a release

1. Land all changes on `main` via reviewed PRs (see [CONTRIBUTING.md](CONTRIBUTING.md)).
2. Bump `version` in `tildagon.toml` (monotonic integer) and update `CHANGELOG.md`.
3. Tag the commit and push the tag:
   ```sh
   git tag v<N>
   git push --tags
   ```
4. Create the release from that exact tag:
   ```sh
   gh release create v<N> --notes-file CHANGELOG.md
   ```
5. Publish a checksum manifest for the shipped files (see below) and attach it to the release.

Tag from a clean `main` only - never from a local working tree with uncommitted changes. Consider signing your tags (`git config tag.gpgSign true`) so the tag's provenance is verifiable, not just its contents.

## Checksum manifest

Generate a manifest of every shipped file at release time and attach `SHA256SUMS.txt` to the GitHub release:

```sh
find app.py metadata.json uQR.py nocturnation shows -type f \( -name '*.py' -o -name '*.json' \) \
  | sort | xargs shasum -a 256 > SHA256SUMS.txt
gh release upload v<N> SHA256SUMS.txt
```

## Verifying a flashed badge

Anyone can confirm the code on a badge matches a published release. Because the app is plain source, this is a direct comparison - no reproducible-build toolchain required.

1. Check out the release tag locally:
   ```sh
   git fetch --tags && git checkout v<N>
   ```
2. Pull the installed app off the badge and diff the payload against the tag.
   The app-store install directory is named after the repo:
   ```sh
   mpremote cp -r :apps/nocturnation_tildagon ./badge-copy
   for p in app.py metadata.json uQR.py nocturnation shows; do diff -r "$p" "badge-copy/$p"; done
   ```
   No differences across the payload means the badge is running exactly the
   published source. (The store also copies the repo's dev files into the same
   directory; those are inert and don't affect the running app.) For a
   `deploy.sh` install the directory is `:apps/nocturnation` instead.
3. Or verify against the published manifest:
   ```sh
   ( cd badge-copy && shasum -a 256 -c ../SHA256SUMS.txt )
   ```

Any mismatch means the badge is running modified or unofficial code - re-deploy from a clean checkout of the tag (`./deploy.sh`).

## Notes

- Only release from the canonical repository. If you fork and publish, make that clear in your `tildagon.toml` `url`/`author` so users know whose build they are flashing.
- The EMF app-store crawler only indexes public repos; see the publishing steps in the [README](README.md).
