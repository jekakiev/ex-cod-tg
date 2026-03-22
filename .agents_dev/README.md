# ex-cod-tg agent notes

- Keep user-facing release notes in one file only: `CHANGELOG.md` in the repo root.
- For every shipped change, add a new top section for the version if needed, then append 1-4 short bullets that a Telegram user would understand.
- Keep bullets concise, product-facing, and specific. Avoid internal implementation detail unless it changes behavior.
- The bot uses the latest version section from `CHANGELOG.md` in update notifications after a bot self-update.
- When preparing a release, update both `bot/version.py` and `pyproject.toml`.
