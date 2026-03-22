# Changelog

## 0.1.4

- Telegram replies now stream live preview text while Codex is thinking, instead of staying on a static `Thinking…` placeholder until the end.
- Bot update flow now asks for confirmation, shows progress in Settings, and reinstalls the service instead of only restarting it.
- Settings and navigation callbacks acknowledge taps faster and avoid expensive repo and branch scans on unrelated pages.
- Update notices are now more reliable after background service restarts.
- Model and thinking switches now stay inline on the dashboard and under replies, with the old dedicated model page removed.
- In-bot updates now reuse the same installer flow as the public `install.sh` command to avoid drift between manual and Telegram updates.
- Quick model and thinking taps now keep the full dashboard menu intact instead of replacing it with a tiny controls row.
- Added a macOS menu bar helper with bot status, start/stop controls, log access, and a launch-at-login toggle.
- In-bot updates on macOS now hand off the final service reinstall to a separate launchd updater job so new helpers also appear after update.
- Fixed the macOS tray helper startup crash and switched Codex streaming to chunk-based parsing so long JSON events do not break live replies.
- Codex replies now surface partial text earlier from incomplete JSON chunks, and update progress stays below 100% until the restarted bot comes back online.

## 0.1.3

- Selected models now come from the live local Codex CLI catalog and are shown two per row.
- Thinking levels now follow the current model, including `xhigh` (`Very detailed`) where supported.
- Home screen is lighter and faster, with less status noise and shorter-lived caches for snappier taps.

## 0.1.2

- Added quick model and thinking switches on the home screen and under each Codex reply.
- Added a selected-models settings page so quick switching only cycles through your preferred models.
- `/start` now always sends a fresh main menu message, and `Update bot` is always visible in Settings.

## 0.1.1

- Added a dedicated model menu with quick switching for the active Codex model and thinking level.
- Bot update notifications now show release notes instead of only the latest commit message.
- Home and Settings now surface update status more clearly when a newer version is available.

## 0.1.0

- Initial public release of the Telegram bridge for local Codex CLI.
- Added repo and branch switching, Codex auth, admin controls, self-update flow, and optional Whisper voice transcription.
