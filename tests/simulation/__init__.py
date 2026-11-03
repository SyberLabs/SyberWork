"""End-to-end campaigns against in-process simulators.

Every campaign binds to 127.0.0.1, speaks the real HTTP, Git, and worker
boundaries, and calls no external host. A production module that is not named
by exactly one campaign fails ``tests.test_simulation``.
"""
