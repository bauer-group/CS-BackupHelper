"""BackupHelper — BAUER GROUP central backup engine.

The core knows HOW to move bytes safely (dump / tar / manifest+sha256 /
S3 multipart / retention / notify / schedule / restore). Consuming repos
register WHAT the bytes mean via Source plugins and lifecycle hooks.
"""

__version__ = "0.1.0"
