import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "mocked: no GPU required, all external calls mocked")
    config.addinivalue_line("markers", "live: requires running llama.cpp inference server")
