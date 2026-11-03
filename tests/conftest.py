"""Strict harness verification must not silently accept skipped tests."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--no-skips", action="store_true", help="fail the run if tests are skipped")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption("--no-skips"):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter and reporter.stats.get("skipped"):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
