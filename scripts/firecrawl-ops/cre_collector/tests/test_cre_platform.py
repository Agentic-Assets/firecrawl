"""The runtime refuses unsupported platforms without emulating POSIX locks."""

import pytest

from cre_platform import UNSUPPORTED_MESSAGE, require_supported_platform


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_supported_platform(platform):
    require_supported_platform(platform)


@pytest.mark.parametrize("platform", ["win32", "cygwin", "freebsd14", "unknown"])
def test_unsupported_platform_refuses_with_actionable_message(platform):
    with pytest.raises(SystemExit) as exc:
        require_supported_platform(platform)
    assert str(exc.value) == UNSUPPORTED_MESSAGE
