# Final Fix Report

## Scope

Implemented only the two requested minor maintainability fixes for Windows auto-update work:

1. `src-tauri/src/app_updates.rs`
   - Removed the broad `allow(unreachable_code, unused_variables)` suppression from `install_app_update`.
   - Kept the no-update DTO unchanged.
   - Made the post-install success path explicitly divergent via `restart_after_update(app) -> !`, so the command no longer fabricates an `installed: true` return after paths that restart or exit the app.

2. `frontend/src/App.tsx` and `frontend/src/components/SettingsDrawer.tsx`
   - Extracted shared install flow into `frontend/src/lib/appUpdates.ts`.
   - Preserved existing behavior:
     - confirmation before install
     - persistent loading toast while installing
     - desktop-unavailable info toast when API returns `null`
     - no success claim after installed path if app exits/restarts
     - no-update result message shown as info
     - errors shown as error toast
   - Kept update UI placement and startup update-check semantics unchanged.

## Verification

Executed the requested commands:

- `cd frontend; npm run test:ui-contract`
- `cd frontend; npm run build`
- `cd src-tauri; cargo test app_updates`

All passed after one frontend source-level contract adjustment to keep the expected literal `api.installAppUpdate()` call pattern at both call sites while still using the shared helper.

## Notes

- Added `ToastContextValue` export from `frontend/src/components/PageToast.tsx` so the shared helper can depend on the existing toast contract without duplicating types.
- No behavior changes were introduced beyond the requested maintainability cleanup.
