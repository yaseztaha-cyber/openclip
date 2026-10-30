"""
Repo test infrastructure: run native `async def` tests via the installed
`anyio` pytest plugin (no new dependencies, no test rewrites).

Any async test without an explicit marker is executed on asyncio. This
fixes collection of pre-existing async tests (e.g. test_final_editor)
without altering application behavior.
"""
import inspect

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def pytest_collection_modifyitems(items):
    for item in items:
        fn = getattr(item, "function", None) or getattr(item, "obj", None)
        if fn is not None and inspect.iscoroutinefunction(fn):
            if "anyio" not in item.keywords and "asyncio" not in item.keywords:
                item.add_marker(pytest.mark.anyio)
