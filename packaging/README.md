# Packaging notes

This directory contains the macOS and Windows packaging helpers for Knowledge Distiller. The
published application is intended for Apple Silicon Macs running macOS 14 or
later. The public GitHub Releases page is the source of truth for downloadable
application packages and supported platforms.

## Local build

Install the build extras in an isolated environment:

~~~sh
uv venv
uv pip install --python .venv/bin/python -e '.[mac,build,secondary]'
~~~

Build the program into an empty disposable directory. Docling weights are a
separate, content-addressed component; neither platform embeds them in its base:

~~~sh
PYTHONPATH=src .venv/bin/python scripts/build_mac.py \
  --version <YYYY.MM.DD.N> --output <empty-output-directory>
~~~

The build must not include user databases, Obsidian Vaults, browser profiles,
model caches, credentials, logs, or private signing keys. Run the freeze/runtime
checks from the build scripts using an independent data directory.

## Running a packaged app

Keep the complete application bundle together. The application stores its
runtime data in the user's application-data directory and writes source notes
only to the Vault selected by that user. Do not use a production database or a
personal Vault for build or test verification.

## Update signing

The update configuration contains only the public update-verification key and
the public GitHub feed URL. The corresponding private signing key must remain in
a local secure key store and must never be committed, copied into a release
directory, or uploaded.

The update preparation script creates signed metadata and archives but does not
upload them. Verify the package, update metadata, hashes, and release notes
before publishing a GitHub Release.

## Third-party resources

The application uses third-party libraries, fonts, models, and packaging
components. Their notices and licenses remain separate from this project's
Apache-2.0 license; retain those files when redistributing a package.

## Windows and paired candidate builds

See [Windows build instructions](WINDOWS_BUILD.md) for the pinned model manifest,
Windows dependency lock, native tools, packaging and verification steps.
[Paired candidate builds](DUAL_PLATFORM_BUILD.md) describes optional two-host
coordination using a private configuration derived from
[the example](dual-build-config.example.json). No script publishes automatically.

The source reconciliation does not rebuild or replace existing Releases. Record
the actual candidate commit and native verification before releasing new binaries.

## Component candidate flow

The application and installer share `DoclingComponent`, the signed release parser,
download cache and assembly verifier. Build models with
`scripts/package_docling_component.py` from separately verified input; build each
platform base with `scripts/package_platform_base.py`. The base contains runtime
code and platform resources (including Windows Paddle models), while the common
Docling weights remain external. Qwen stays optional and independent.

Use `scripts/build_component_installer.py` on each native platform. Verify actual
frozen runtime, offline PDF/EPUB, old full-bundle model import, installation and
rollback before publishing. These scripts alone do not constitute release acceptance.
The older `package_mac.py` and `package_windows.py` create local validation archives;
they are not the ordinary component release upload list.

Runtime checks the component at startup in the background and before conversion.
Missing or corrupt models show a repair instruction; no implicit model download
is performed. Existing data and prior model versions remain intact.

For offline installation, put the signed `release-<platform>.json` and its named
assets in one directory and select that manifest in the installer. The same
publisher signature, inventory and target checks apply; missing assets stop the
operation without falling back to the network. Already verified cache entries
can also be reused. `prepare_component_release.py` validates local build provenance
and asset bytes before using the existing publisher signing identity. Uploading
and switching Latest remain separate release steps.

Sparkle attachment reconstructs its SDK from the pinned archive on each build,
so changed or flattened unpacked framework trees cannot become build inputs.

Component-enabled app metadata selects `ComponentUpdates`. Its settings UI uses
the shared signature/parser/download plan; the copied standalone update helper
rechecks the manifest, reconstructs and verifies the candidate before asking the
reserved application instance to exit. A process/token check binds that request
to the initiating app. The existing rollback journal and paused startup accept
only a matching build with a ready document component. Old XML clients keep their
old protocol; they require a separately verified migration installer path.
