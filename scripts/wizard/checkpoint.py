import json
from pathlib import Path

# Set by main() after HOME_DIR is known; mirrors initialize.py's CHECKPOINT_PATH pattern.
CHECKPOINT_FILE = ".init-checkpoint.json"

CHECKPOINT_PATH = None  # set after HOME_DIR is known


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH and CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text())
    return {"step": "start"}


def save_checkpoint(data: dict):
    if CHECKPOINT_PATH:
        CHECKPOINT_PATH.write_text(json.dumps(data, indent=2))


def clear_checkpoint():
    if CHECKPOINT_PATH and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
