# Authenticate ChatGPT in the user's daily browser

Amendment (2026-09-29): The user accepted one-time manual sign-in in the
separate managed browser as the default onboarding path, avoiding a required
extension installation. A valid managed login is reused without prompting.
The daily-browser extension remains an optional way to transfer an existing
ChatGPT session. The managed window uses an installed Chromium/Chrome/Edge
executable with a private profile; it does not import the daily browser's
passwords, Passkeys, or profile.

The user originally required first sign-in and reauthentication to reuse their daily browser's Google / Passkey experience. That path remains available, while the separate interactive login window from ADR-0016 is the default. The managed runtime remains independent of the daily browser for model requests. An already authenticated runtime session must not be treated as a requirement to sign in again.

The optional daily-browser path uses a user-clicked Chromium extension to hand only `chatgpt.com` cookies to an authenticated same-origin Runtime Settings request. It has no Google host permission, background collector, password access, or Passkey export. Installing this extension in the user's daily browser remains an explicit user step; extension installation and real daily-browser handoff have not yet been accepted.

The runtime validates a candidate session through its pinned browser before selecting it. A generation pointer records the saved account, while the active runtime's configuration keeps referencing its old account until the user explicitly restarts. Verification failures keep the old selection; saving never restarts a process. Authentication success is not evidence of connector/tool readiness. Accounts in the browser and runtime are not continuously synchronized: expiration or switching accounts requires another explicit connection.

Opening the default browser alone cannot transfer HttpOnly cookies. Copying a live browser profile, decrypting its credential database, or enabling debugging on the daily profile is not part of this design. Firefox/Safari support is not implemented.
