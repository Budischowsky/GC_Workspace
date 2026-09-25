"""Application data travels with the project; shared directories remain optional.

GCWS-PATCH: vendored from NIAS Working. ROOT is the standalone application
folder (two levels above this file) and DATA follows ``GCWS_DATA`` when set.
The one-time migration from the old per-user folders is not run here: the
standalone starts with its own data folder.
"""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ['GCWS_DATA']) if os.environ.get('GCWS_DATA') else ROOT / 'data'
RESOURCES = ROOT / 'resources'


def initialize():
    for folder in (DATA, DATA / 'reports', DATA / 'projects'):
        folder.mkdir(parents=True, exist_ok=True)


def settings_path():
    if not DATA.exists():
        initialize()
    return DATA / 'settings.json'


def project_path(value):
    path = Path(value)
    if path.is_absolute():
        return path
    for base in (ROOT, RESOURCES, DATA):
        if (base / path).exists():
            return base / path
    return ROOT / path
