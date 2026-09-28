# Authenticate ChatGPT in the user's daily browser

The user requires first sign-in and reauthentication to reuse their daily browser's Google / Passkey experience. This supersedes ADR-0016's separate interactive login window; the managed runtime remains independent of the daily browser for model requests. An already authenticated runtime session must not be treated as a requirement to sign in again.

The implementation uses a user-clicked Chromium extension to hand only `chatgpt.com` cookies to an authenticated same-origin Runtime Settings request. It has no Google host permission, background collector, password access, or Passkey export. Installing this extension in the user's daily browser remains an explicit user step; extension installation and real daily-browser handoff have not yet been accepted.

The runtime validates a candidate session through its pinned browser before selecting it. A generation pointer records the saved account, while the active runtime's configuration keeps referencing its old account until the user explicitly restarts. Verification failures keep the old selection; saving never restarts a process. Authentication success is not evidence of connector/tool readiness. Accounts in the browser and runtime are not continuously synchronized: expiration or switching accounts requires another explicit connection.

Opening the default browser alone cannot transfer HttpOnly cookies. Copying a live browser profile, decrypting its credential database, or enabling debugging on the daily profile is not part of this design. Firefox/Safari support is not implemented.
