"""The agent's sandbox directory, shared by every tool that takes a path.

config_upgrade_code sets it to the per-run temp dir. Tools resolve the paths the
model hands them against it, so a relative name like "repo" lands inside the
sandbox rather than wherever the server process happens to be running, and a
path that would escape the sandbox is refused.
"""

import os

_WORKING_DIR = ""


def set_working_dir(path):
    global _WORKING_DIR
    _WORKING_DIR = os.path.realpath(path)


def get_working_dir():
    if not _WORKING_DIR:
        raise RuntimeError("Working dir not configured; set_working_dir() must run at startup.")
    return _WORKING_DIR


def resolve(path):
    """Absolute real path for *path* inside the sandbox; ValueError if it escapes."""
    root = get_working_dir()
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(candidate)
    if real != root and not real.startswith(root + os.sep):
        raise ValueError(
            f"{path} is outside the working directory {root}; "
            "use a path relative to it (for example repo/pom.xml)"
        )
    return real


def relative(path):
    """*path* expressed relative to the sandbox, for messages to the model."""
    return os.path.relpath(resolve(path), get_working_dir())
