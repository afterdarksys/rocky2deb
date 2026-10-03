# rocky2deb

Current state: library 0.1.0 is in this tree and the fixture suite is green. The initial library is commit `a4f7e84` on `origin/main`. `debian2rocky`, `ubuntu-exporter`, unsigned source-RPM packing, dnf metadata, Debian archive fetch, and pinned repack are in this tree. On 2026-10-03, `PYTHONPATH=src /usr/local/bin/python3.11 -m unittest discover -s tests` reported `Ran 107 tests` and `OK`. The runners for sbuild, mock, ssh, apt, and HTTPS fetch are in the library. That run injected fakes. This Mac has no sbuild, mock, rpm, or ssh, so the run did not launch those tools or open a network connection. Fixture bytes stayed in temporary directories. The packed source RPM round-trips `extract_src_rpm`. The published primary metadata round-trips `parse_primary_xml`. This Mac did not prove that rpm or mock accepts either file.

`build`, `fetch`, `apply`, `suite-upgrade`, `inventory-collect`, and `rockify build` act only with `--execute`. A builder that exits 0 without a new `.deb` or `.rpm` is a failure. A write to `/` is refused unless `--allow-live-root` is set. The next action is one real sbuild on a Debian test host. That run has not happened.

## What the four programs do

One git-tracked project, one shared artifact repository, many hosts. Four console scripts share `src/rocky2deb/`:

- `rocky2deb` plans a Rocky 8, 9, or 10 fleet onto a Debian suite and converts package source.
- `rockify` answers one question: given an RPM name and a Debian suite, what has to be built or shimmed before that program can run.
- `debian2rocky` plans a Debian host onto a fresh Rocky 8, 9, or 10 install, emits an RPM spec, packs an unsigned source RPM, publishes dnf metadata, and fetches one Debian archive source.
- `ubuntu-exporter` exports one Ubuntu source package (a `.dsc`, an orig tarball, and a debian tarball) into a Debian suite or a Rocky spec.

`rocky2deb` and `rockify` share one resolver so a license gap and a base-package denial cannot diverge between fleet planning and a single-app build. `debian2rocky` uses the reverse resolver. The forward order is unchanged.

`rocky2deb init` creates the project directories and `rocky2deb.toml`. A second init refuses when that file already exists, so a re-init cannot change `target_suite` out from under recorded pins. The project stores decisions. It does not store copied private keys or config values.

## Feature status

| Request | Where it lives | This version |
| --- | --- | --- |
| Migrate Rocky hosts to Debian | inventory, waves, gates, apply | Collect and apply run only with `--execute`. Apply copies a bundle under `--root` and refuses `/`. |
| Rocky source to a Debian source package | `import-spec`, `emit-deb`, `srpm`, `dscpack` | Spec text or a `.src.rpm` in, `debian/` tree and a packed `.dsc` out. Packing does not run `debian/rules`. |
| Binary RPM from a Debian source | `emit-rpm`, `repack`, `build rpm` | Spec text for autotools, cmake, meson, python. `emit` still raises spec-unemittable for `dh` and custom. A pin whose build system is exactly `dh` or `custom` is `repack` and writes a noarch source-install spec. `build rpm --execute` launches mock when mock is installed. |
| Config remap | `remap` and `data/maps/` | Baseline maps. Report prints keys, never values. |
| Suites stretch through trixie | `profiles.py` | Emitter and planner profiles. The 107-test run built no chroot. |
| Binary deb from a Red Hat source RPM | `emit-deb`, `build deb`, `rebuild` | Packs a `.dsc`, then `sbuild --chroot-mode=unshare` when `--execute` is set and sbuild is installed. |
| Waves between sets of hosts | `gates`, `verify` | Canary, then the rest. Offline verify compares an inventory. A live host needs `--execute`. |
| License-gap rebuild | ledger, resolver, `fetch` | Decision is computed. `fetch --execute` stores an SRPM only after the checksum and `gpgv` pass. |
| Move an already-Debian fleet to a newer suite | `retarget`, `suite-upgrade` | Without `--execute`, plan only. With `--execute` and `--root`, writes the suite list and runs apt-get. `/` is refused. |
| One RPM name onto Debian | `rockify plan`, `rockify build` | Closure and shims. Build runs only with `--execute` and refuses a blocked root before that check. |
| Fresh Rocky replay of a Debian host | `debian2rocky` | Read-only Debian inventory. Reverse resolver. Reverse maps. Apply copies a bundle onto a Rocky 8, 9, or 10 root, refuses a non-Rocky id, and refuses `/`. The copy does not run dnf. |
| Ubuntu source into our distro | `ubuntu-exporter` | Plan prints the retargeted version and the index file names. Export requires a caller license, then `gpgv` on the local Sources bytes, then SHA256 of each pool file. |
| Unsigned source RPM | `debian2rocky pack` | Copies spec and source bytes into an unsigned RPM v4 `.src.rpm`. The 107-test run extracted it with `extract_src_rpm`. rpm and mock were not run. |
| dnf repository | `debian2rocky publish` | Copies `.rpm` files and writes `repodata/primary.xml.gz`. Does not sign. Does not run createrepo or dnf. The 107-test run parsed it with `parse_primary_xml`. |
| Debian archive source | `debian2rocky fetch-source` | Local signed Sources, then SHA256 of each pool file. The archive version and the changelog are kept. Requires `--execute`. |

## Locked decisions

Cutover is a fresh Debian install, then replay. Inventory of the Rocky host is read-only. The bundle is applied on a new or reinstalled Debian host. The running Rocky root stays intact until the operator retires it. An in-place conversion of a live RPM root can leave a machine that is neither RPM nor dpkg; a fresh install has a known Debian base.

The resolver prefers Debian. A Rocky SRPM is rebuilt only for a license gap, a license holdout the ledger allows, or an explicit pin. The operator confirmed fresh-install replay and the baseline config set. The Debian-by-default policy was locked after an empty answer on that question and was not corrected.

Shipped config maps are the Rocky baseline: sshd, NetworkManager, firewalld, chrony, hostname, sysctl, journald, sudo, and cron. Any other path stays unmapped until the operator adds a map. Forward remap rewrites a Rocky path onto the Debian path and is idempotent. Reverse remap rewrites a Debian path onto the Rocky path and is idempotent. Both reports print keys and the destination path. Values stay out of the report.

The controller runs on bookworm or newer. Stretch and buster are target profiles only.

## Package IR

Every conversion goes through Package IR version 1.

1. Import a `.spec` or a `debian/` tree. The IR keeps Enterprise Linux paths.
2. Emit a Debian source tree or an RPM spec. Debian paths are computed here from the suite profile.
3. Build, with `--execute`, inside the suite chroot. `sbuild --chroot-mode=unshare` builds a `.dsc`. `mock --rebuild` builds a `.src.rpm`. The controller packs the `.dsc` in Python and does not call `dpkg-source`, because that runs `debian/rules clean`. A missing tool, a non-zero exit, or a zero exit with no new package file refuses. The 107-test run produced no package.

`emit-deb` writes `debian/control`, `debian/rules` as a `dh` sequence, `debian/copyright` pointing at the license field, and a quilt `debian/patches/series`. It does not invent license text or patch bodies. The original `%prep`, `%build`, and `%install` scripts are copied into `debian/README.source` and are not executed. A name or a `%files` path that contains `..` is refused before any write, because the emit destination is a directory tree.

`emit-rpm` has a template for autotools, cmake, meson, and python. A rules file is classified cmake when the text contains cmake, then meson, then python, then `buildsystem=autotools` or `buildsystem=autoconf`, then a `dh` word, otherwise custom. Plain `dh $@` stays `dh`. A `dh` or custom build system raises spec-unemittable on `emit`. `repack` is a separate action. It is returned only when the operator pins the name and the build system is exactly `dh` or `custom`, after base and trademark. An omitted build system stays `pin-rebuild`. The spec is noarch, lists the source tarballs, and installs the unpacked tree under `/usr/src/repack/%{name}`. `%build` is empty. `debian/rules` is not executed.

Changelog timestamps are the fixed string `Sat, 03 Oct 2026 00:00:00 +0000`. Two emits of the same IR must match byte for byte, so the date is not "now".

## Resolver

`resolve_one` walks this order and stops at the first hit. Pins and the ledger default to empty unless the caller loads them. Trademark branding is still caught by name without the ledger.

1. Base denylist, even when the name is pinned. The Debian installer already provides these. Rebuilding them would replace libc, systemd, the kernel, or the package manager.
2. Trademark (the logos and backgrounds packages). Always a gap. Branding is not ours to rebuild.
3. Explicit rebuild pin, when the license class allows it.
4. Curated map in `data/names.toml`, when that Debian package is in the suite index and meets an optional version floor. The floor is compared against the Debian version of the mapped package.
5. The same upstream name in the suite index.
6. Rebuild from the Rocky SRPM when the license class allows it.
7. Gap, with the reason that blocked the earlier steps.

Actions are `debian`, `rebuild`, `pin-rebuild`, `gap`, and `base`. `package_list` includes the deb names for debian, base, pin-rebuild, and rebuild. It omits gaps.

License classes are `dfsg`, `non-free`, `trademark`, and `unknown`. `classify_text` returns `dfsg` only for a known free expression. Anything else, including an empty string, `Proprietary`, and a mix of free and proprietary, is `unknown`. Unknown blocks the artifact. The classifier never guesses `non-free` or `trademark`. A non-free rebuild happens only when the ledger sets `allow_rebuild`. Redis is the ledger example: Debian ships valkey, and a Rocky redis rebuild is non-free and allowed only by that flag. `rocky-logos` stays a gap even if the text says MIT, the name is pinned, and the Debian index contains it.

When a later suite starts shipping a package, a plain `rebuild` flips to Debian. A `pin-rebuild` stays `rebuild` with the reason `pin still requests a rebuild`. A pin means the operator rejected Debian's package on purpose. Retarget must not drop that decision because the name appeared in the index.

## Suites

Numbers live in `profiles.py`. `ProfileTests` is the check that they still match this table. Stretch and buster use `https://archive.debian.org/debian`. Bullseye through forky use `https://deb.debian.org/debian`. Every profile's amd64 libdir is `/usr/lib/x86_64-linux-gnu`. Emit rewrites a `/usr/lib64` prefix to that libdir and a `/usr/libexec` prefix to the profile libexecdir. There is no system-wide `/usr/lib64` symlink. The rewrite happens inside the emitted package.

| Suite | Debian | Role | debhelper | usrmerge | glibc ceiling | libexecdir |
| --- | --- | --- | --- | --- | --- | --- |
| stretch | 9 | archived target | compat file 10 | no | 2.24 | `/usr/lib` |
| buster | 10 | archived target | compat file 12 | no | 2.28 | `/usr/lib` |
| bullseye | 11 | oldoldstable | `debhelper-compat (= 13)` | no | 2.31 | `/usr/lib` |
| bookworm | 12 | oldstable, controller ok | `debhelper-compat (= 13)` | yes | 2.36 | `/usr/libexec` |
| trixie | 13 | stable | `debhelper-compat (= 13)` | yes | 2.41 | `/usr/libexec` |
| forky | 14 | testing, opt-in | `debhelper-compat (= 13)` | yes | 2.43 | `/usr/libexec` |

Compat 13 is a build dependency because a `debian/compat` file is the wrong form at that level. Stretch and buster still get the compat file and `debhelper (>= N)`. Forky is not one of the five required suites. It is a legal `target_suite` for retarget. Trixie time64 is real on 32-bit; amd64 is the first architecture this project emits.

Rocky glibc in the same file is profile data: 8 → 2.28, 9 → 2.34, 10 → 2.39. The Rocky 10 figure was not re-checked against a live repo when this doc was written. Re-check it before a binary-mode decision on el10.

A source that cannot compile on an old suite is `unbuildable` only when a compiler log is supplied. The Debian source package is still emitted. Rockify then blocks that package when the log is non-empty and the package has no patch. Do not block a rebuild merely because Rocky's glibc is newer than the suite.

Adding the next Debian release is a new profile row, a column on the name map if units or names differ, and a `ProfileTests` update. That is the whole change.

## Config remap

Maps are declarative: Rocky path, Debian path, format, critical flag, known keys, and the packages they belong to. Project files in `maps/config/` override the packaged map with the same id.

- `copy`, `xml`, `keyfile`, `text`, and `cron-spool` keep the body and report nothing. NetworkManager keyfiles stay keyfiles. Do not parse them as line-oriented INI.
- sshd, chrony, and sysctl split lines. A non-empty `known` list reports any other key, keeps that line in the body, and copies it aside. An empty `known` list keeps every key and reports none.
- sshd rewrites the sftp subsystem from `/usr/libexec/openssh/sftp-server` to `/usr/lib/openssh/sftp-server` on every suite, including bookworm where libexecdir stays `/usr/libexec`.
- chrony moves `/etc/chrony.conf` to `/etc/chrony/chrony.conf`.
- cronie user crontabs move `/var/spool/cron/<user>` to `/var/spool/cron/crontabs/<user>`. Translating a path that is already on the Debian side leaves it there.
- firewalld, hostname, journald, sudoers, and sysctl stay on their existing paths. SELinux booleans are recorded only. This version does not invent an AppArmor profile.

The printed report is `map <id> -> <debian path>` and `unmapped <key>`. Values stay out of the report because config bundles contain secrets. A critical unmapped key fails `pre_apply`. A new project's `plan/waves.toml` sets `critical_maps` to sshd and network.

## Fleet

1. Group hosts into `canary` and `rest`. A canary that is not in the host set is refused. Order of the remaining hosts is preserved.
2. `pre_apply` fails while any required decision is `gap`, or while a critical map has an unmapped key. The reasons name the gaps and the keys.
3. The rest wave waits until `pre_apply` has passed and the canary `verify` has passed.
4. `apply` and `apply-check` read `os-release`. They print `ok` only when `ID=debian` and `VERSION_CODENAME` is the target suite. Any other id or suite is refused. `apply` without `--execute` stops there. With `--execute` it also needs `--root` and `--bundle`, copies the manifest into that root, and refuses `/` unless `--allow-live-root`. The copy does not run apt.
5. `verify` needs `--inventory` or `--host`. An offline inventory prints `missing package`, `missing unit`, or `missing listener` and exits 3 when any are absent. A host verify without `--execute` refuses. `inventory-collect` without `--execute` refuses. `ssh-argv` still only prints the command.

`ssh-argv` prints `ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new <host> bash -s`. The host is one argument. It must match `[A-Za-z0-9_.:-]+` and must not start with `-`. A rejected host is not echoed. The command does not connect. The offline collector format is `PKG` lines of seven tab fields and `UNIT` lines of three. Epoch `(none)` or `0` becomes empty.

## Debian to Debian

Rocky to Debian stays a fresh install. Debian to Debian is the one in-place path.

`retarget --to forky` selects the new suite and re-resolves. With `--project`, it rewrites `target_suite` in `rocky2deb.toml` and leaves `rocky_major` and `pins` as they were. Without `--project` it only returns rows.

`suite-upgrade --from bookworm --to trixie` refuses when the host is not Debian or the host suite is not `--from`. On a matching host the diff is:

- `base` → `keep`
- `pin-rebuild` → `rebuild` (`pin still requests a rebuild`)
- `rebuild` whose deb or rpm name is in the new index → `flip-to-debian`, otherwise `rebuild`
- `debian` still present → `keep`, missing → `drop`
- `gap` now present → `flip-to-debian`, still missing → `blocked`

Without `--execute` the command prints `plan-only` and does not run apt. With `--execute` it requires `--root`, refuses a symlink root, and refuses `/` unless `--allow-live-root`. It writes `etc/apt/sources.list.d/<suite>.list` and runs `apt-get update`, an install of the rebuild deb names, and `apt-get full-upgrade -y`. A root other than `/` gets `apt-get -o Dir=<resolved-root>` so the controller apt database stays untouched. Stretch and buster source lines include `[check-valid-until=no]`. The command does not rebuild the pins itself and does not reboot. The 107-test run injected a fake apt runner.

## Rockify

Rockify is possible for a leaf program whose dependencies Debian ships, or that rebuilds against the target suite's libc. It is not a way to install Rocky's glibc, systemd, kernel, kernel modules, or rpm/dnf stack on Debian.

1. Load repodata (`primary.xml` or a JSON fixture) and map the binary name to its source RPM.
2. Walk `Requires`. `Recommends` are included only with `--weak`. Drop `rpmlib()`, `config()`, absolute file requires, and names that start with `(`.
3. Classify each dependency with the resolver above. Build-requires that are themselves gaps are ordered first.
4. Topo-sort the `rebuild` and `pin-rebuild` nodes. Ready nodes drain in alphabetical order. A cycle raises `CycleError` naming the leftover nodes and stops that closure.
5. Emit a compat source package only when the report is not `blocked` and at least one shim exists.

The report is `runs`, `runs-with-shims`, or `blocked`. The plan is blocked when the root is `kernel`, a `kernel-*` or `kmod-*` name, a `*-kmod`, `glibc`, `systemd`, `rpm`, `dnf`, or `yum`; when the license is unknown or trademark; when a compiler log is present and the package has no patch; or when a cycle includes a blocked package. A dependency that is itself a kernel package is `base` and does not block the application. A missing dependency that resolves to `gap` blocks the plan with that gap's reason.

`--binary` compares ELF `GLIBC_2.x` versions to the suite ceiling. A symbol newer than the ceiling sets the binary field to `blocked` and tells the operator to rebuild from source. The source order and the report stay as they were. Binary mode never installs a Rocky binary over Debian libc.

Shims are minimal and per app:

- A unit alias only when the decision is `debian` and the Rocky unit name differs from the Debian unit. A rebuilt package keeps its own unit, so a pin-rebuild of httpd gets no alias.
- A config-path shim when a baseline map names the package and the Rocky path differs from the Debian path.
- An sftp shim for `openssh-server` unless that decision is a gap.

The compat package is `rockify-compat-<name>`, Architecture `all`, Provides `rockify-<name>`. Its `Depends` are the resolved deb names for `debian`, `rebuild`, and `pin-rebuild`. `dh_link` lines are written only for unit aliases, as `source destination`. The unit source is `/usr/lib/systemd/system` when the profile has merged `/usr`, and `/lib/systemd/system` otherwise. The alias destination is `/etc/systemd/system/<unit>`. Notes record the shim kind and the two paths. Control files do not contain config values.

`rockify build` uses that same plan. A blocked report refuses before the `--execute` check, so a glibc root still names the Debian installer. Any other plan without `--execute` refuses to build. With `--execute`, `--sources`, and `--output`, it builds each name in order with sbuild and then writes the compat source when shims exist. The library allows an empty order without `--execute`. The CLI does not, so a build is not aimed at the current directory.

## Debian to Rocky

Cutover is a fresh Rocky 8, 9, or 10 install, then replay. Inventory of the Debian host is read-only. The bundle is applied on a new or reinstalled Rocky host. The running Debian root stays intact until the operator retires it.

`resolve_deb` walks this order and stops at the first hit. The Rocky index maps a binary name to a version. The version is not compared. Pins and the ledger default to empty unless the caller loads them.

1. Base, even when the name is pinned and even when it is in the Rocky index. `libc6` is `glibc`. `apt` is `dnf`. `dpkg` is `rpm`. `linux-image-amd64` is the kernel. A companion such as `libc6-dev` uses the same base RPM. `aptitude` is not base.
2. Trademark, by the Debian name, the mapped RPM name, or a ledger class of `trademark`. Always a gap. `rocky-logos` and `ubuntu-mono` stay gaps when pinned.
3. Explicit pin, when the license class allows it. Exactly `dh` or `custom` returns `repack`. Any other build system, including an omitted one, stays `pin-rebuild`. Base and trademark are decided before the pin.
4. Curated reverse of `data/names.toml`, when that RPM is in the Rocky index. The reason is `curated map`. `apache2` maps to `httpd`. `nginx` maps to `nginx`.
5. The same name in the Rocky index. The reason is `same name in rocky index`.
6. Rebuild from the Debian source when the license class allows it. The RPM name is the mapped name when one exists, otherwise the Debian name.
7. Gap, with the reason that blocked the earlier steps.

Actions are `rocky`, `rebuild`, `pin-rebuild`, `repack`, `gap`, and `base`. `rocky_package_list` keeps the RPM names for rocky, base, pin-rebuild, rebuild, and repack. An indexed install does not require a license class. A pin and a rebuild do.

Inventory lines are `PKG` with five tab fields (package, version, architecture, source) and the same three-field `UNIT` lines. Epoch `(none)` or `0` becomes empty. A source field of `hello (2.10-1)` stores the name `hello`. A seven-field Rocky `PKG` line is `unknown inventory line`. `inventory-collect` without `--execute` refuses. `ssh-argv` prints the same command as `rocky2deb` and does not connect. A rejected host is not echoed.

`apply` reads `os-release`. It prints `ok` only when `ID` is exactly `rocky` and the major of `VERSION_ID` is `--rocky`. `ID=Rocky` is refused. `9` does not match `90`, and `09` does not match `9`. Without `--execute` the command stops after that check. With `--execute` it needs `--root` and `--bundle`, copies the manifest into that root at mode `0644`, and refuses `/` unless `--allow-live-root`. The copy does not run dnf.

`emit` writes an RPM spec from a `debian/` tree. The build system is checked first, so `dh` and custom rules are spec-unemittable even when the license text is empty. A trademark name is refused before an empty license. The license line is the caller's text. `debian/copyright` is not read, and `debian/rules` is not executed. `pack` copies the spec and extra source bytes into an unsigned RPM v4 source package. The signature SHA1, SHA256, and MD5 tags are format fields the writer computes. They are not a trust decision. `publish` copies those files into a dnf repo and writes `repodata/primary.xml.gz`. It does not sign and it does not run createrepo. `fetch-source` reads a local Debian Sources index. It requires `--execute`, checks `gpgv`, then SHA256, and writes `debian/` plus the orig tarball. It does not rewrite the changelog and it does not apply the Ubuntu version policy. `build` checks that mock exists before it requires a `.src.rpm`. Without `--execute` it refuses. The 107-test run extracted a packed source RPM with `extract_src_rpm` and parsed primary metadata with `parse_primary_xml`. It did not launch rpm or mock.

Reverse chrony moves `/etc/chrony/chrony.conf` back to `/etc/chrony.conf`. Reverse sshd rewrites the sftp subsystem from `/usr/lib/openssh/sftp-server` to `/usr/libexec/openssh/sftp-server`. A path that is already on the Rocky side stays there. The printed line is still `map <id> -> <path>`.

## Ubuntu source export

`ubuntu-exporter` reads a local Ubuntu `Sources` index and one source package name. It exports source files. It does not export binary debs. The archive is `https://archive.ubuntu.com/ubuntu`. Known suites, in table order, are jammy 22.04, noble 24.04, oracular 24.10, plucky 25.04, questing 25.10, and resolute 26.04. An unknown suite is refused with that list in that order.

The default distro is Debian, and `--suite` must be a profile from `profiles.py`. `--distro rocky --rocky 9` uses the same export as input to `debian2rocky`. Any other distro is refused with `distro must be debian or rocky`.

`plan` does not download and does not create a destination. It prints the retargeted version, then one `<sha256> <size> <name>` line per index file.

Version policy:

- An Ubuntu delta (the version contains `ubuntu`) onto Debian becomes `<version>~<debian suite>1`. `2.10-3ubuntu1` onto trixie is `2.10-3ubuntu1~trixie1`.
- A Debian sync, with no `ubuntu` token, onto Debian becomes `<version>~ubuntu.<ubuntu suite>.1`, which sorts before the archive revision.
- Either form onto Rocky becomes `<version>~el<major>1`.
- The epoch stays in the changelog `Version`. Filenames drop it. `1:2.10-3ubuntu1` onto trixie is published as `hello_2.10-3ubuntu1~trixie1.dsc` and the `.dsc` still says `Version: 1:2.10-3ubuntu1~trixie1`.
- A version with whitespace, `/`, `..`, parentheses, or a character outside ASCII 32..126 is `refusing version`. A folded Sources continuation that would inject another field fails the same way, and the injected text is not echoed.

`export` checks the archive URL first, so a URL with userinfo is refused before the `--execute` check and the URL is not echoed. Without `--execute` the command refuses. With `--execute` the order is the caller's `--license`, then `gpgv` on the local Sources bytes, then SHA256 of each pool file. `Proprietary` and a mix such as `MIT and Proprietary` are `license unknown` and stop before any download. A missing or symlinked signature, keyring, or Sources file refuses without echoing the path. MD5 in the `Files` field is `.dsc` compatibility and is not the trust check. A declared size above the cap, a body longer than the declared size, or a digest mismatch is `checksum mismatch`. The destination directory is created only after the debian tarball is unpacked, the first changelog version is rewritten, and the result is packed. A symlink member, a path that escapes `debian/`, or a changelog package that does not match the source writes nothing there.

A base package or a trademark name, on the requested source or on its `Binary` list, refuses before download. Another stanza in the same Sources file does not. The unpacked `debian/rules` is not executed. Member modes drop setuid. Regular files are `0644` and `debian/rules` is `0755`.

A Debian export republishes a `Format: 3.0 (quilt)` `.dsc`, the orig tarball bytes, and a new `debian.tar.xz`. The new checksums are the SHA256 of the bytes written. A Debian export with no orig tarball refuses. A Rocky export may omit the orig tarball and publishes one `.spec` for an emittable build system. It does not publish an `.rpm`. A `dh` or custom tree is spec-unemittable and leaves the destination absent. The 107-test run injected the downloader and `gpgv`. It did not contact `archive.ubuntu.com` or `archive.debian.org`.

## Safety rules the tests lock

- Checksums are compared with `hmac.compare_digest`. A mismatch, a missing checksum, or a short read refuses before the destination file is created. A `.partial` sibling is renamed into place only after the checksum and the signature both pass. Why: a failed download must not become the artifact, and a non-constant compare is banned.
- URLs are `https` with a hostname. Userinfo is refused and the URL is not included in the error. Why: userinfo is a credential, and a cleartext fetch can be replaced in transit.
- Detached signatures are checked with `gpgv` and a pinned keyring. An empty signature, a missing keyring, or a non-zero `gpgv` refuses. A remote SRPM is stored only when the checksum, the signature, and the keyring are all present. A local spec import does not need them.
- `primary.xml` is capped (default 50 MiB) and refused when the text contains a doctype or an entity, before the parser runs. Why: entity expansion is a parser bomb.
- Downloads use the default TLS context, a size cap, and a timeout of 1 to 120 seconds. `fetch` without `--execute` refuses. With `--execute` it requires `--sha256`, `--signature`, `--keyring`, and `--output`. A missing or symlinked signature or keyring refuses without echoing the path. The body is written to a `.partial` sibling and renamed only after the checksum and `gpgv` both pass. A download error does not include the URL. Why: a failed or redirected fetch must not become the artifact.
- `build` without `--execute` refuses. The tool check runs before `--source` and `--suite` are required, so a missing sbuild still exits 3 with `sbuild is not installed; refusing to pretend the build succeeded`. An installed tool is launched with its absolute path as argv[0]. sbuild gets `--chroot-mode=unshare`. mock gets `--root rocky-{8|9|10}-x86_64 --resultdir DIR --rebuild`. A non-zero exit, a timeout, or a zero exit that did not create a new `.deb` or `.rpm` refuses. `%build` and `%prep` never run on the controller. The `.dsc` packer does not execute `debian/rules`.
- `import-spec` of a path ending in `.src.rpm` extracts it. A missing file refuses with `src.rpm is missing`. A `..` name, an absolute name, a symlink member, a symlink destination, and a zstd payload refuse. Zstd needs the `zstd` program on the controller. The declared member bytes and the decompressed stream stop at the size cap with `src.rpm exceeds size cap`. `rpm2cpio` is not required.
- `apply` and `suite-upgrade` write only under an explicit `--root`. `/` and a symlink root are refused. `suite-upgrade` without `--execute` stays plan-only.

Exit codes: a `Refused` condition, including spec-unemittable and a dependency cycle, is exit 3. Other `Rocky2debError` results are exit 1. Messages go to stderr as `rocky2deb: ...`, `rockify: ...`, `debian2rocky: ...`, or `ubuntu-exporter: ...`.

## Check

1. Use Python 3.11 or newer. The `python3` on the Mac that produced this doc is 3.9 and has no `tomllib`. The green run used `/usr/local/bin/python3.11`.
2. From the repo root, run `PYTHONPATH=src /usr/local/bin/python3.11 -m unittest discover -s tests`.
3. Done when the last lines are `OK` and the count is 107. A failure is not done. A zero exit from sbuild or mock with no new package file is not a successful package.

## Next action

On the Debian test host (bookworm or newer, with sbuild and mmdebstrap), pack the hello fixture into a `.dsc` and run one real `sbuild --dist <suite> --arch amd64 --chroot-mode=unshare --no-run-lintian` of that dsc. Record the exit code and whether a new `.deb` appeared in the result directory. The 107-test run on this Mac is not that build. This Mac produced no package. That run proved `extract_src_rpm` and `parse_primary_xml` only.
