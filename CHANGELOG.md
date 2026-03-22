# Changelog

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
