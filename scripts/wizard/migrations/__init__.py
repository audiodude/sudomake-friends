"""Migrations system for sudomake-friends.

Each migration is a subdirectory here with a `migration.py` that exposes:
    ID: str
    TITLE: str
    MANDATORY: bool
    DESCRIPTION: str
    def is_needed(friends_dir: Path) -> bool
    def run(friends_dir: Path, interactive: bool = True) -> bool

Migrations run in ID-sorted order. Applied IDs are tracked in
~/.sudomake-friends/.migrations-applied so each one only runs once per user.

To author a new migration: copy the most recent migration directory,
bump the timestamp prefix in both the directory name and the ID constant,
and implement is_needed + run.
"""
