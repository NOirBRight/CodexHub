# ADR-0017: Separate ChatGPT Runtime Settings from Provider Connection

Date: 2026-09-27
Status: Accepted responsibility boundary and notification-only restart behavior

Tracking: [settings separation specification #584](https://github.com/NOirBRight/CodexHub/issues/584), supplementing #567.

This refines [ADR-0016](0016-managed-chatgpt-web-runtime.md). The user confirmed
that CodexHub continues to install, start, and manage the pinned original
ChatGPT Web Runtime. Its complete configuration belongs in a separate settings
webpage opened through an external-browser link from CodexHub, using the same
managed instance. ChatGPT login, tunnel configuration, Runtime Key, connector
authorization, and runtime options belong to that settings surface.

The CodexHub Provider Connection page owns the runtime service address, service
access credential, connection checks, model discovery/selection, and Gateway
routing. Locally managed connection values should be filled automatically where
possible, with a manual editing entry. The settings-page URL and model-service
address are distinct, as are service access credentials and tunnel/account
credentials. This avoids maintaining competing copies of upstream configuration
inside CodexHub's provider editor.

Saving a setting that requires a component restart only persists the change and
notifies the user that the ChatGPT Web Runtime needs a restart. Saving must not
wait for active tasks, schedule a deferred restart, or actually restart the
runtime or Gateway. The user handles the restart separately. Until the new
configuration is loaded, the UI must distinguish saved settings from active
settings and must not report the pending change as already effective.

This is a target boundary, not evidence that an upstream settings web server
already exists or that connector setup is implemented. The current three-stage
native onboarding and read-only connector diagnostic do not fulfill it.
Implementation and acceptance evidence remain outstanding.

Source inspection at pinned upstream commit
`a13cd09950969f43e3b7e25c71fa43efaf5446c5` finds that
`launcher/src/App.tsx` requires `window.codexWebLauncher`, supplied by Electron's
`launcher/electron/preload.cjs`. The main process loads the packaged page with
`loadFile`. Opening its HTML in an ordinary browser therefore does not provide
working settings: a browser-accessible control adapter is needed to reuse that
surface. This source finding does not change the accepted external-browser UX.
