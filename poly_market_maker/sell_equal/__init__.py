"""Legacy sell-only helper: equal gross USD, not equal shares.

Paper and replay run without credentials. This is not the share-balanced
session mode. Live trading is a separate opt-in adapter and is not started
by importing this package.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
