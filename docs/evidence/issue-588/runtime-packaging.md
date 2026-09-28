# ChatGPT Web Runtime package provenance

CodexHub must package the Web Runtime that implements the authenticated
`/admin/status` v1 contract. The published v6.1.1 binaries are based on the
same upstream release number but do not implement that contract, so they are
not a download fallback.

The source pin remains the public `miuuyy/codex-chatgpt-web` repository at
`a13cd09950969f43e3b7e25c71fa43efaf5446c5` (v6.1.1). The reviewed combined
patch is
[`codex-chatgpt-web-control-contract.patch`](../issue-590/codex-chatgpt-web-control-contract.patch),
SHA-256 `6e89c190cd6d01126a3d64d52bf09cb6a5fde8f24361f37e3056f8d1729104e1`.
Applying it to the public commit produces Git tree
`641caaf875fcf908dfaa919c242f24fdede26a69`; the resulting source revision is
`93b8e6fc3eda8a81176964be87f8c7b8fc637a7f`. It retains the model selection
repair and protected read-only admin contract, and attests successful-turn
cookie rotation to a server-confirmed account key plus the exact storage-state
digest. Bun is pinned to 1.4.0; dependency installation is frozen and lifecycle
scripts are disabled.

Run `scripts/prepare_chatgpt_web_runtime.py` with the repository Python
launcher before creating a platform package. It fetches only the exact public
source commit, verifies the combined patch and resulting Git tree, builds and
smoke-tests the relocatable runtime, then writes its archive and a generated
platform-only pin under `src-tauri/resources/chatgpt-web-runtime/`. Set
`CODEXHUB_BUN_EXECUTABLE` when Bun 1.4.0 is not on `PATH`. Linux and Windows
portable and release builders invoke this preparation automatically. A
prebuilt archive can be supplied through `CODEXHUB_CHATGPT_WEB_RUNTIME_ARCHIVE`;
the current Linux portable script also accepts `--chatgpt-web-runtime` for this
purpose. Prebuilt inputs must match the reviewed platform checksum.

The generated pin uses `bundled_only: true`, includes the archive checksum, and
has no download URL. If that packaged archive is missing, install or upgrade
fails with a clear error instead of downloading the upstream v6.1.1 binary.
Only Linux x64 and Windows x64 are currently supported by this patched runtime;
other architectures fail closed.

The paired Linux x64 and Windows x64 source builds for the account-attestation
revision completed on 2026-09-28 from CodexHub candidate SHA
`2740520e40793459ff4af0a2fccba76296506f10`. Both use runtime revision
`93b8e6fc3eda8a81176964be87f8c7b8fc637a7f`, tree
`641caaf875fcf908dfaa919c242f24fdede26a69`, and Bun 1.4.0. Each build passed
typecheck and both the in-place and extracted relocatable runtime smokes.

The Linux archive SHA-256 is
`e370f6916e1d9e4bc60af81ef79269f16970e34c985db3f625e20c891dfc2782`; the
Windows archive SHA-256 is
`5cbb11d6d848018d1f079c89151f4dd46cc7d5ebb83a878970626b5120b35a95`. The
source pin records both platform checksums and the shared runtime revision and
tree. These are prepared candidate artifacts, not a published CodexHub
release. Packaging smokes do not establish real account login, connector
authorization, or client tool calls.
