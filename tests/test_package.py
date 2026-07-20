"""Sanity check that the adsync package is importable and installed correctly."""

import adsync


def test_package_has_version() -> None:
    assert adsync.__version__ == "0.1.0"
