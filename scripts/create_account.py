"""Create a new ThreadsBot account from the template.

Usage:
    python scripts/create_account.py <account_id>
    python scripts/create_account.py haircare
    python scripts/create_account.py --list           # List existing accounts
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_ACCOUNTS_DIR = _PROJECT_ROOT / "accounts"
_TEMPLATE_DIR = _ACCOUNTS_DIR / "_template"

_VALID_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

_MUST_CUSTOMIZE = [
    "config/tone.yaml",
    "knowledge/profile.yaml",
    "knowledge/target.yaml",
    "knowledge/genre.yaml",
    "knowledge/theme_tree.yaml",
    "knowledge/domain_knowledge.md",
    "config/ng_words.txt",
]


def list_accounts() -> list[str]:
    """Return existing account IDs (excluding _template)."""
    if not _ACCOUNTS_DIR.exists():
        return []
    return sorted(
        d.name
        for d in _ACCOUNTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )


def create_account(account_id: str) -> Path:
    """Create a new account directory from the template.

    Returns the path to the created account directory.
    """
    # Validate account_id
    if not _VALID_ID_RE.match(account_id):
        print(
            f"[ERROR] Invalid account ID: '{account_id}'",
            file=sys.stderr,
        )
        print(
            "  Must be lowercase alphanumeric + underscore, 2-31 chars, "
            "starting with a letter.",
            file=sys.stderr,
        )
        sys.exit(1)

    if account_id in ("default", "all"):
        print(
            f"[ERROR] '{account_id}' is a reserved name.",
            file=sys.stderr,
        )
        sys.exit(1)

    target_dir = _ACCOUNTS_DIR / account_id
    if target_dir.exists():
        print(
            f"[ERROR] Account directory already exists: {target_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not _TEMPLATE_DIR.exists():
        print(
            f"[ERROR] Template directory not found: {_TEMPLATE_DIR}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Copy template
    shutil.copytree(_TEMPLATE_DIR, target_dir)

    # Rename .env.example -> .env
    env_example = target_dir / ".env.example"
    env_target = target_dir / ".env"
    if env_example.exists():
        env_example.rename(env_target)

    # Ensure data directories exist
    for subdir in ("data/state", "data/analytics", "data/archive", "data/backups"):
        (target_dir / subdir).mkdir(parents=True, exist_ok=True)

    return target_dir


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)

    if sys.argv[1] == "--list":
        accounts = list_accounts()
        if accounts:
            print("Existing accounts:")
            for a in accounts:
                print(f"  - {a}")
        else:
            print("No accounts found.")
        sys.exit(0)

    account_id = sys.argv[1].lower()
    target_dir = create_account(account_id)

    print(f"[OK] Account '{account_id}' created at: {target_dir}")
    print()
    print("Next steps:")
    print(f"  1. Edit .env:")
    print(f"     {target_dir / '.env'}")
    print()
    print("  2. Customize these files:")
    for f in _MUST_CUSTOMIZE:
        print(f"     {target_dir / f}")
    print()
    print("  3. Verify:")
    print(f"     python scripts/preflight.py --account {account_id}")
    print()
    print("  4. Run:")
    print(f"     python -m core.scheduler all --account {account_id}")


if __name__ == "__main__":
    main()
