# Packaging notes

This directory contains the macOS packaging helpers for Knowledge Distiller. The
published application is intended for Apple Silicon Macs running macOS 14 or
later. The public GitHub Releases page is the source of truth for downloadable
application packages and supported platforms.

## Local build

Install the build extras in an isolated environment:

~~~sh
uv venv
uv pip install --python .venv/bin/python -e '.[mac,build,secondary]'
~~~

Prepare the pinned document-processing resources in a disposable directory, then
build into another empty disposable directory:

~~~sh
PYTHONPATH=src .venv/bin/python scripts/prepare_docling_models.py \
  --output <docling-models-directory>
PYTHONPATH=src .venv/bin/python scripts/build_mac.py \
  --docling-models <docling-models-directory> \
  --output <empty-output-directory>
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
