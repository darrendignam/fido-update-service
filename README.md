# fidosigs

Format signature update service for [fido](https://github.com/openpreserve/fido). It serves
PRONOM signature releases to `fido -sigs check | update | list | vNNN`.

| | |
| --- | --- |
| Service | <https://fidosigs.opf-labs.org> |
| Legacy production | <https://fidosigs.openpreservation.org> (2022 image, v109, until cutover) |
| Image | `ghcr.io/darrendignam/fidosigs` |
| Operations | [docs/operations.md](docs/operations.md): deploy, publish a PRONOM release, roll back |

## API

All responses are XML except downloads. Trailing slashes are accepted directly, with no
redirect, because fido sends them.

| Path | Returns |
| --- | --- |
| `/` | service list |
| `/format` | every release, oldest first |
| `/format/latest` | `<signature version="vNNN"/>`, the number fido compares against |
| `/format/{NNN\|vNNN}` | file list for one release |
| `/format/{NNN\|vNNN\|latest}/{fido\|droid\|pronom}` | `formats-vNNN.xml`, `DROID_SignatureFile-vNNN.xml`, `pronom-xml-vNNN.zip` |

## How releases are served

A release is a directory `vNNN/` holding exactly those three files. The service lists the
format directory on every request, so a new directory is live immediately: no rebuild, no
restart. Anything not matching `v<digits>` is ignored, including the `.vNNN.partial`
staging directory the generator writes before its atomic rename.

The format directory is `FIDOSIGS_FORMAT_DIR` when set, otherwise the copy bundled in the
image at `fidosigs/resources/format/`. Production mounts a host directory, so the data is
not tied to an image build. That coupling is why the service sat on v109 from 2022 to 2026.

## Layout

```text
fidosigs/main.py                  FastAPI app (entrypoint fidosigs.main:APP)
fidosigs/resources/format/vNNN/   bundled releases, v70..latest
scripts/generate_signatures.py    builds vNNN/ from PRONOM, non-interactive
deploy/                           compose stack: web + cloudflared (profile tunnel)
.github/workflows/                test, publish, update-signatures, check-live
```

## Develop

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                  # 60 tests, offline, includes fido 1.6.1's own client
uvicorn fidosigs.main:APP --reload      # http://127.0.0.1:8000/format/latest
```

## CI

| Workflow | Trigger | Does |
| --- | --- | --- |
| `test` | PR, push to master | pytest, compose files parse |
| `publish` | push to master, tag `vX.Y.Z` | test, build, smoke test fido's paths, push to GHCR |
| `update-signatures` | monthly, manual | generate the newest PRONOM release, open a PR |
| `check-live` | weekly, manual | fail if the live service lags PRONOM |

Image tags: `latest`, `sha-<commit>` and `pronom-vNNN` from master; `X.Y.Z` and `X.Y` from tags.

## Constraints worth knowing

- **fido 1.6.1's own updater no longer works.** `fido.update_signatures` calls PRONOM's SOAP
  service over `http://`, PRONOM now answers 307, and urllib will not follow a redirected
  POST. `scripts/generate_signatures.py` goes direct to https and reuses only fido's
  PRONOM-to-fido conversion (`fido.prepare.FormatInfo`).
- **Conversion is reproducible.** Regenerating `formats-v109.xml` from the 2022
  `pronom-xml-v109.zip` gives identical output apart from 3 md5 checksums of external
  reference files that have since changed upstream.
- **fido mis-converts offset windows (upstream bug, not fixed here).** `fido.prepare` writes
  `.{Offset,MaxOffset}`, but PRONOM's MaxOffset is relative: DROID matches
  Offset..Offset+MaxOffset. In v125 this narrows 95 BOF windows across 60 formats, and 6
  regexes do not compile at all (fmt/1558, 1646, 1669, 1670, 1738, 2115), so fido cannot
  match those formats by signature. The service publishes fido's output unchanged; the
  generator logs affected PUIDs. The fix belongs in `openpreserve/fido`.
- **No container signatures.** fido pins `container-signature-UPDATE-ME.xml` for manual
  handling, so `/container` was a stub and has been removed.
- **Alpine base image.** Debian-based Python images cannot start threads under the seccomp
  profile of Docker < 20.10.10 (clone3), which is what the OPF build box runs.
- **PRONOM throttle.** The generator waits 0.5 s between record requests: about 30 minutes
  for 2,571 formats. Do not parallelise it.

## History

- 2022: Flask to FastAPI rewrite in PRs #1 to #3, never merged. Production ran a hand-built
  image from the tip of that stack.
- 2026-09: stack merged, v99 to v109 recovered byte-for-byte from the production image (they
  were never committed), v125 generated, CI, host-mounted data, Cloudflare Tunnel deployment.
