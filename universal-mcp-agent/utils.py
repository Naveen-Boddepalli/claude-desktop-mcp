import os
from config import MAX_OUTPUT, ALLOWED_ROOTS


def is_safe_path(path: str) -> bool:
    # Expand ~ and normalize
    path = os.path.expanduser(path)
    abs_path = os.path.abspath(path)

    allowed = [
        os.path.abspath(os.path.expanduser(root))
        for root in ALLOWED_ROOTS
    ]

    return any(abs_path.startswith(root) for root in allowed)


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    return text[:limit] + ("\n...[truncated]" if len(text) > limit else "")