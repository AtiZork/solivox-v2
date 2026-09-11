"""Pytest configuration."""

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: hits live Shyft network endpoints")
