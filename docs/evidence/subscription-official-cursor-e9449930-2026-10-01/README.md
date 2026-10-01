# Official → Cursor V2 qualification on e9449930

One authorized, bounded actual Codex CLI 0.159.3 case passed on exact production
candidate `e9449930e3cc6e5c8812a2862471b1ca88c36a5d`. The source runtime was clean;
its frozen snapshot SHA-256 was
`4c715365c9f190e72d884f6e2aa3402cf7d0010e8a3b4a5c41f6296970cd13b8`.
The case took 72.996 seconds, within the 180-second case / 240-second total
bounds. No other case or vendor retry was run in this qualification.

The Official parent selected exact `gpt-6-astra`; its single Cursor child
selected exact `cursor-subscription/gpt-5.6-luna-high`. All 15 observed Responses
requests returned HTTP200 (nine Official and six Cursor). All six Cursor
requests omitted independent reasoning effort. No model fallback, account
login, refresh, replacement codec or fabricated caller tool result was used.
The Official token was checked before the run to remain valid beyond its
entire bound. Accounts were copied only into disposable private homes.

The actual parent used native Collaboration Calls to spawn one child, wait and
follow up with that same child. It did not read the fixture itself. The child's
actual Code Mode custom `exec` Calls read the controlled random fixture twice,
producing the original value and its reversal through real caller execution.
The parent returned exactly those two lines. Spawn and follow-up targets shared
canonical address SHA-256
`809db9fc2e57755f847923e3e38b112ff88bac7f65037ee041ad1436b8c83cc0`.
The child rollout SHA-256 was
`454683a6b215e2228b966a996bb3f40c26858165168793318e476ba5ce7e4d7e`.

All nine observed native Collaboration Calls had
`encrypted_function_args:[]`. All 26 recorded agent-message content sequences
contained only `input_text`; none contained `encrypted_content`. These records
prove the live source-prevention/inverse path after the
[compatibility-route correction](../subscription-native-v2-source-prevention-2026-10-01.md).

The harness stopped the actual Gateway, started a new Gateway process loading
the same frozen snapshot, and invoked a fresh Codex caller with completed
parent history. Its completed Calls/results were replayed, and it returned
exactly the original reversed fixture value without new fixture reads.
All hard checks passed. Equality used the original strings; no zero-width
characters or failed outputs were stripped, interpreted or repaired.

Both the case's private account/rollout tree and the outer candidate snapshot
were removed, and removal was checked after cleanup. No matching private probe
trees remained. The [sanitized qualification artifact](qualification.json)
retains request status, exact selected IDs, metadata shapes, Call/Item and
address hashes, child execution proofs, restart/replay checks and cleanup flags.
It contains no copied auth, dynamic prompts, raw task assignments or ciphertext.

The earlier [e13e2fd0 Official → Cursor failure](../subscription-codemode-v2-e13e2fd0-2026-10-01.md)
remains unchanged as historical evidence. Code Mode, Cursor → Official and
completed-history cases were not repeated on e9449930; their earlier successful
records remain tied to their own exact candidate snapshot. Root owns the final
review, complete local gates and PR integration.

Reproduction uses the existing public harness, with a clean integrated checkout
and explicitly authorized accounts:

```sh
CODEXHUB_PYTHON=/path/to/compatible/development/python \
TMPDIR=/path/to/private/temp \
./scripts/codexhub-python.sh scripts/qualify_subscription_codemode.py \
  --checkout /path/to/clean/candidate \
  --source-codex-home /path/to/official/source/home \
  --source-user-home /path/to/source/user/home \
  --case official-to-cursor --case-timeout 180 --total-timeout 240 \
  --output /path/to/sanitized/qualification.json
```
