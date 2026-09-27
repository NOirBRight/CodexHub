# Claude family mapping and explicit selection identity

Date: 2026-09-27. Installed Claude Code: 2.1.283.

## User-observed regression

With `ANTHROPIC_DEFAULT_FABLE_MODEL=claude-codexhub-gpt-6-astra`,
the user selected **CodexHub 6 Astra** with arrow keys and Enter in `/model`.
Claude Code displayed **Switch to Fable?** and a zero-credits gate. Removing
only this family mapping and opening a new Claude Code session removed the
gate; the user confirmed the result and the saved selection was Astra.

Removing the mapping is only a diagnostic workaround: it loses the intended
internal Fable-to-Astra mapping. The required behavior keeps that mapping,
explicit Astra selection, and explicit native Fable selection separate.

## Corrected wire identities

Family environment values use a separate, non-picker identity. For Astra:

| Client selection | Wire model | Gateway target |
| --- | --- | --- |
| Internal `fable` alias | `claude-codexhub-role/fable/gpt-6-astra` | `gpt-6-astra` |
| Explicit Astra | `claude-codexhub-gpt-6-astra` | `gpt-6-astra` |
| Explicit native Fable | `claude-fable-5-1` | Native Fable |

The same separation was checked for OpenCode Go DeepSeek V4.1 Flash.
Six real CLI invocations passed the request-identity assertions using synthetic
authentication and a loopback capture inside a separate network namespace:

```sh
bwrap --unshare-net --ro-bind / / --dev-bind /dev /dev --proc /proc --tmpfs /tmp \
  ./scripts/codexhub-python.sh scripts/probe_claude_role_identity.py \
  --claude-bin /path/to/claude
```

The capture deliberately returns HTTP 400 after reading the model ID.
Expected CLI exit status is 1; the probe exits 0 only when all captured IDs and
Gateway alias resolutions match. No real credential or provider is used.
This proves the installed CLI's family resolution and explicit-ID preservation,
not successful inference, internal-task coverage, or account credits UI.

Python HTTP seam tests additionally verify that both explicit and family IDs
reach the same upstream model, while unavailable/unknown family targets fail
closed. Rust configuration tests verify distinct picker/environment IDs,
legacy-value migration, idempotent republish, and canonical role readback.

The same-account interactive credits check with the corrected IDs remains
required before claiming the original popup is fixed in an installed build.
