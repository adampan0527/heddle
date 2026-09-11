# SPDX-License-Identifier: Apache-2.0
"""heddle_common — shared Python library for heddle CLI and daemon.

Per TECH.md T-014. Sub-modules:
- logging: structured JSON logger with redaction (T-017)
- feature_list_io: schema-versioned read/write of `feature_list.json`
  (T-014, T-022 / feat-009)
- fake_llm: scripted-response LLM used when HEDDLE_FAKE_LLM is set
  (feat-007 / T-018, T-031)
- configs_io: read/write/manage `~/.heddle/configs.yaml` (feat-011 /
  T-023, D-053, D-055, T-015)
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

# Re-export the fake-LLM API so callers can do `from heddle_common import
# FakeLLM, fake_llm_or_real` without reaching into the submodule.
from .fake_llm import (  # noqa: F401
    FAKE_LLM_ENV_VAR,
    DEFAULT_FIXTURE_DIR,
    FakeLLM,
    FakeLLMExhausted,
    ScriptedResponse,
    ScriptedToolCall,
    fake_llm_or_real,
    is_fake_llm_enabled,
    load_fixture,
)

# Re-export the configs_io API so callers can do
# `from heddle_common import Config, load_configs` etc. The CLI
# `heddle configs list` command and the daemon's LLM config
# resolution (feat-023, feat-031) both import from this surface.
from .configs_io import (  # noqa: F401
    ALLOWED_PROVIDERS,
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_TEMPLATES,
    Config,
    ConfigsError,
    add_config,
    default_configs_path,
    delete_config,
    ensure_configs,
    get_config,
    list_configs,
    load_configs,
    resolve_api_key,
    resolve_api_key_for_config,
    save_configs,
    update_config,
)

# Re-export the projects_io API so callers can do
# `from heddle_common import Project, add_project` etc. The CLI
# `heddle projects add | list | remove` command (feat-050 +
# feat-012) and the daemon's per-project bootstrap (feat-014 /
# feat-018) both import from this surface.
from .projects_io import (  # noqa: F401
    DEFAULT_PROJECTS_DIR,
    DEFAULT_PROJECTS_PATH,
    MAX_PROJECTS,
    MAX_PROJECTS_BYTES,
    PROJECTS_SCHEMA_VERSION,
    PROJECTS_SCHEMA_VERSION_MAX,
    Project,
    ProjectsError,
    add_project,
    default_projects_path,
    list_projects,
    load_projects,
    remove_project,
    save_projects,
    touch_project,
)

# Re-export the env_loader API so callers can do
# `from heddle_common import load_env_file` etc. Both the CLI
# (`heddle start` / `heddle env status`) and the daemon bootstrap
# call load_env_file() once at startup to merge ~/.heddle/.env
# into the process environment (T-015, feat-013).
from .env_loader import (  # noqa: F401
    DEFAULT_ENV_DIR,
    DEFAULT_ENV_PATH,
    DEFAULT_GITIGNORE_PATH,
    EnvParseError,
    EnvPermissionError,
    describe as describe_env,
    ensure_env_loader,
    load_env_file,
    parse_env_lines,
)