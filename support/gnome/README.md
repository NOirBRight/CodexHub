# GNOME menu property loading compatibility

On the tested Ubuntu GNOME 50 AppIndicators extension, layout readiness can
cancel the layout cancellable before the deferred GetGroupProperties query.
CodexHub exports the correct labels, but GNOME caches empty defaults.
Reloading the extension only temporarily hides this initialization race.

The attached patch ties CodexHub property queries to the menu proxy lifetime,
so later layout refreshes cannot cancel their initial text load. The normal
proxy shutdown still cancels them. It applies only to the CodexHub D-Bus menu
path; other indicators and global extension preferences are unchanged.
It also removes the earlier hardcoded dark label color so menu text follows
the GNOME theme in both light and dark mode.
This is a desktop extension compatibility patch, not an application CSS fix.
The application and installer do not silently install it.

Before applying, back up the installed dbusMenu.js and check the patch against
the installed extension version. A fresh GNOME session is required to
reliably load changed JavaScript modules; disabling/re-enabling an extension
may reuse cached modules. Restore the backup and start a fresh session to
roll back. An OS package update may replace this local patch.

Verification: isolated GNOME 50 Wayland reproduced seven empty labels with
the original file. With the patch, all seven labels were present on first
launch and after two consecutive application restarts. The same test also
checks the rounded transparent window, shadow, and four complete provider
cards at 1076×820 and 110% scale.
