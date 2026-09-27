# ChatGPT Web Runtime package provenance

CodexHub must package the Web Runtime that implements the authenticated
`/admin/status` v1 contract. The published v6.1.1 binaries are based on the
same upstream release number but do not implement that contract, so they are
not a download fallback.

The source pin remains the public `miuuyy/codex-chatgpt-web` repository at
`a13cd09950969f43e3b7e25c71fa43efaf5446c5` (v6.1.1). The reviewed combined
patch is
[`codex-chatgpt-web-control-contract.patch`](../issue-590/codex-chatgpt-web-control-contract.patch),
SHA-256 `50577ded0e5b012ec7ea893f98dcd1635705470c0f093e4cb192c9cadd638c35`.
Applying it to the public commit produces Git tree
`2d49d1dca11aa21a340348bb2a356c4078ef50ab`; the resulting source revision is
`9c2892af646f36752cc131dedd90af6586e6e4ce`. It retains the prior model
selection repair and adds the protected read-only admin contract. Bun is pinned
to 1.4.0; dependency installation is frozen and lifecycle scripts are disabled.

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

The paired control-contract artifacts prepared for this candidate were:

| Platform | SHA-256 |
| --- | --- |
| Linux x64 | `ab118da7d08baae8d1cd8496a2951e6e613a8827ccdc411a11ba52ba36a62c1d` |
| Windows x64 | `780bbb9b63888379cc41c77ba5dc293a30d93d375a4d0c98af659629bb04ec0e` |

These are packaging provenance and empty-account/runtime smoke inputs. They do
not establish real account login, connector authorization, client tool calls,
or final Linux/Windows application package qualification.
