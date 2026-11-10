# Changelog

## Public recovery release — 2026-10-25

Based on five archived Python tools from May 2026. The final private archive's logs differ from the surviving scripts, so this release is not presented as the exact historical pipeline.

### Retained

- The recovered Tkinter photo-review interface and index gallery.
- Index-image matching, a human confirmation step, numbered-folder review and audit modes.

### Changed for publication

- Removed private account configuration, invite links, historical message-ID lists and machine-specific paths.
- Added configurable archive paths and a synthetic offline demo.
- Split grouping/state/file operations from the desktop interface.
- Added explicit opt-in for Telegram downloads/comparison and Cloud Vision requests.
- Completed dependency declarations and separated optional network integrations.
- Added recursive batch discovery and duplicate-location reporting.
- Applied the requested Telegram group argument and supported legacy `review` log records.
- Corrected OCR numeric boundaries, retry-on-error behaviour and content-aware cache identity.
- Added destination validation, collision preservation, batch rollback and a recovery journal.
- Made review CSV writes atomic and recorded actual destination names.
- Added offline regression tests and a desktop smoke-check script.

Original private media, logs, inventories, sessions and credentials are intentionally excluded. No historical Git history has been fabricated.
