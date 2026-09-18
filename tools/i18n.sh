#!/usr/bin/env bash
# Translation catalogue workflow.
#
# The -F flag is not optional: without it pybabel extracts from .py files only,
# so strings that appear only in templates are missing from the template and an
# update marks their translations obsolete.
set -euo pipefail

cd "$(dirname "$0")/.."

POT=pressledger/locales/messages.pot
LOCALES=pressledger/locales

# A string that leaves the code is kept as an obsolete `#~` entry, so a renamed
# label can reuse its translation. To purge them:
#   uv run pybabel update -i "$POT" -d "$LOCALES" -l de --ignore-obsolete
VERSION=$(uv run python -c 'import tomllib; from pathlib import Path; print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])')
uv run pybabel extract -F babel.cfg -o "$POT" \
    --project PressLedger --version "$VERSION" \
    --copyright-holder "Michael Hampicke" \
    --msgid-bugs-address "https://github.com/mgeha/pressledger/issues" \
    --header-comment "# Translations template for PROJECT.
# Copyright (C) YEAR ORGANIZATION
# This file is distributed under the same license as the PROJECT project." .
uv run pybabel update -i "$POT" -d "$LOCALES" -l de
uv run pybabel compile -d "$LOCALES"

echo "Catalogue updated. Review pressledger/locales/de/LC_MESSAGES/messages.po for"
echo "fuzzy entries before committing."
