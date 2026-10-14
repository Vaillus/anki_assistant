"""Every test runs against a temporary collection it creates: point the app's default
collection at a path that does not exist, so that no test can open the user's collection."""

import os

os.environ["ANKI_COLLECTION"] = "/nonexistent/anki-assistant-tests/collection.anki2"
os.environ.pop("ANKIWEB_HKEY", None)
