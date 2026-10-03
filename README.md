# rocky2deb

Plans a Rocky Linux 8, 9, or 10 fleet onto Debian, converts package source in both directions, and replays a cutover bundle onto a fresh install. Version 0.1.0. Python 3.11 or newer.

Cutover is a fresh install, then replay. Inventory of the old host is read-only. The running root stays in place until the operator retires it. A write to `/` is refused unless `--allow-live-root` is set.

Four console scripts share `src/rocky2deb/`:

- `rocky2deb` plans the fleet and converts a Rocky source RPM into a Debian source package.
- `rockify` says what must be built or shimmed before one RPM name can run on a Debian suite.
- `debian2rocky` plans the reverse cutover, emits a spec, packs an unsigned source RPM, publishes dnf metadata, and fetches one Debian archive source.
- `ubuntu-exporter` exports one Ubuntu source package into a Debian suite or a Rocky spec.

`build`, `fetch`, `apply`, `suite-upgrade`, `inventory-collect`, `rockify build`, `ubuntu-exporter export`, and `debian2rocky fetch-source` act only with `--execute`. A builder that exits 0 without a new `.deb` or `.rpm` is a failure. `debian2rocky pack`, `repack`, and `publish` copy bytes. They do not run `debian/rules`, createrepo, or dnf.

The packed `.src.rpm` is unsigned. Its header round-trips this tree's extractor. The dnf `primary.xml.gz` round-trips this tree's parser. Neither check is a run of `rpm` or `mock`.

The locked contract, suite table, and resolver order are in [docs/plans/2026-10-03-rocky2deb-design.md](docs/plans/2026-10-03-rocky2deb-design.md).

## Check

From the repository root:

```sh
PYTHONPATH=src python3.11 -m unittest discover -s tests
```

On 2026-10-03 that command reported `Ran 107 tests` and `OK`. The run injected fakes. It did not launch sbuild, mock, ssh, or a network fetch, and it produced no package.

The next build is one real `sbuild --dist <suite> --arch amd64 --chroot-mode=unshare --no-run-lintian` of the packed hello fixture on a Debian test host (bookworm or newer). That run has not happened.
