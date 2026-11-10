# Validation of the public recovery release

Local validation on Windows with Python 3.12:

- Offline regression suite: 31 tests, covering safe paths, collision preservation, rollback, recursive batches, stale and legacy logs, duplicate locations, image decoding, OCR retry/cache behaviour and mocked Telegram operations.
- Desktop smoke check: generate eight synthetic cards, load the recovered interface in a withdrawn window, assign batches, complete folder/audit passes and load the index gallery. A synthetic HEIC image is included when the HEIF plugin is installed.
- CLI checks: help, offline demo generation, local validation, manifest output, OCR preview and Telegram recovery preview.
- Public-package checks: no credentials, sessions, real archive/log files, local dependency directories or generated Python caches included.

Live Telegram authentication/downloads and live Google Vision requests are not part of this validation. Their boundary behaviour is tested with mocks. The GitHub Actions workflow is supplied for subsequent pushes; no hosted CI run is claimed before publication.

To reproduce:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python tests/gui_smoke.py
```

The GUI smoke check requires a desktop Tk environment; the ordinary regression suite does not create windows.
