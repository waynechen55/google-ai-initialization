#!/usr/bin/env python3
"""
Test runner for Google AI & MCP Workspace Initializer.
Executes all unit and integration test suites in the tests/ directory.
"""
import sys
import unittest
from utils import ensure_bridges_directory_intact

def main():
    ensure_bridges_directory_intact()
    loader = unittest.TestLoader()
    suite = loader.discover("tests", pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

if __name__ == "__main__":
    main()
