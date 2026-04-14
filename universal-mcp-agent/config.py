import os

# Max output size (token control)
MAX_OUTPUT = 2000
MAX_FILES = 20

# Allowed directories (SECURITY)
ALLOWED_ROOTS = [
    os.path.expanduser("~"),  # your home directory
    "/home/claude",
]

# Safe commands only
ALLOWED_COMMANDS = [
    "ls",
    "pwd",
    "head",
    "tail",
    "wc"
]