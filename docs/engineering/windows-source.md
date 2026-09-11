# Windows source integration

The public source now includes the Windows desktop entry point, process ownership
and locks, per-user credential protection, OCR and optional Qwen adapters, and
signed update staging/recovery. macOS keeps its platform-specific entry points.
Shared update checks refuse to replace an application directory that also contains
the configured database or Vault.

Use [Windows build instructions](../../packaging/WINDOWS_BUILD.md) and the checked-in
model manifest to prepare isolated inputs. Native tools and their notices still
need preparation as described there; the Git repository contains no model weights
or private host/signing configuration. The source keeps the public synthetic test
identities and self-test audio.

`.github/workflows/windows-tests.yml` runs native process, lock, DPAPI and update
regressions with synthetic temporary data. This is not a frozen application build,
a model inference test, or verification of an existing Release. Consult the PR
checks for results for the exact commit. Historical binaries may contain fixes
from multiple build stages; this integration does not assert byte-for-byte
reproduction or replace any published asset.
