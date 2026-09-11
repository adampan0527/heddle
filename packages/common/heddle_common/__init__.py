# SPDX-License-Identifier: Apache-2.0
"""heddle_common — shared Python library for heddle CLI and daemon.

Per TECH.md T-014. Sub-modules:
- logging: structured JSON logger with redaction (T-017)
- feature_list_io: schema-versioned read/write of `feature_list.json`
  (T-014, T-022 / feat-009)
"""

__version__ = "0.0.1"

# Re-export the schema_version contract for callers that only import
# the top-level package. The daemon and HARNESS CLI both use these to
# decide whether to refuse loading a too-new file (T-022).
from .feature_list_io import (  # noqa: F401
    SCHEMA_VERSION,
    SCHEMA_VERSION_MAX,
    SchemaVersionError,
)