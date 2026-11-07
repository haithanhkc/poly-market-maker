"""Keep the repository's root pytest run from importing this suite.

The keeper tests live in the top-level ``tests`` package. This project has
its own ``tests`` package. Collecting both in one session puts this directory
first on ``sys.path`` and the keeper then imports the wrong ``tests``.
Run the paper suite from this directory: ``python3 -m pytest -q``.
"""


def pytest_ignore_collect(config, **_kwargs):
    root = getattr(config, "rootpath", None)
    name = getattr(root, "name", "")
    if name != "paper_split_maker":
        return True
    return None
