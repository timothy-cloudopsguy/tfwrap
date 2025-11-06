"""Terraform wrapper tool for managing backends and bootstrapping.

This package provides a CLI tool to manage Terraform remote backends
and bootstrap infrastructure with S3 state storage.
"""

from .version import __version__

__all__ = ["__version__"]
