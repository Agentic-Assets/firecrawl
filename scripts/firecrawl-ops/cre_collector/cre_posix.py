"""Load POSIX lock primitives only after the supported-runtime check."""

from cre_platform import require_supported_platform

require_supported_platform()

import fcntl as fcntl  # noqa: E402 - platform check must precede this import
