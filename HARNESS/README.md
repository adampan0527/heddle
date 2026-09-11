# Long-Running Agent Harness

A system for enabling AI agents to work effectively across multiple context windows on complex, long-running projects.

## Overview

This harness implements the architecture described in Anthropic's article [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents).

### Key Components

1. **Initializer Agent** - Sets up the initial environment on the first run
2. **Coding Agent** - Makes incremental progress in every session
3. **Environment Management** - Structured files for tracking progress and requirements

## Architecture

### Two-Part Solution

The system addresses the challenge of long-running agents with two agent types:

#### 1. Initializer Agent
- Runs once at project start
- Sets up the project structure
- Creates `init.sh` script for development and testing
- Creates comprehensive `feature_list.json` with all requirements
- Initializes git repository
- Creates `current_progress.txt` (active append target, also holds SESSION 0 — see
  `HARNESS.md` § `current_progress.txt`) for documentation

#### 2. Coding Agent
- Runs in subsequent sessions
- Starts each session by reading progress files and git history
- Starts development server and verifies basic functionality
- Works on ONE feature at a time
- Tests features end-to-end before marking as complete
- Commits changes and updates progress file

## File Structure

```
your-project/
├── CLAUDE.md                  # Agent entry point for Claude Code (read first)
├── AGENT.md                   # Agent entry point for non-Claude-Code agents
├── _AGENT.md                  # Document index every agent reads first
└── HARNESS/
    ├── INITIALIZER_PROMPT.md  # Instructions for the first agent session
    ├── CODING_AGENT_PROMPT.md # Instructions for subsequent sessions
    ├── init.sh                # Development server and testing script
    ├── current_progress.txt   # Active append target for every Coding Agent session (Initializer seeds SESSION 0 here)
    ├── sessions/older/        # Archived SESSION blocks rotated by HARNESS/tools/progress_rotate.py
    ├── docs/templates/        # Single source of truth for SESSION block format
    ├── feature_list.json      # Comprehensive feature requirements
    ├── HARNESS.md             # Full operating manual (workflow, file roles, checkpoint protocol)
    ├── tools/                 # Mandatory CLI for feature_list.json + progress bookkeeping
    └── tests/                 # Pytest suite for tools/conftest + helpers
```

## How to Use

### First Session (Initializer)

1. Give the agent the prompt from `INITIALIZER_PROMPT.md`
2. Provide the high-level project requirements
3. The agent will set up:
   - Project structure
   - `init.sh` script
   - `feature_list.json` with all features in `status: pending` (`metadata.failing == total_features`)
   - Git repository with initial commit
   - `current_progress.txt` (Initializer seeds SESSION 0 here, every subsequent session appends here)
     with the initial SESSION 0 documentation

### Subsequent Sessions (Coding Agent)

1. Give the agent the prompt from `CODING_AGENT_PROMPT.md`
2. The agent will:
   - Read progress files and git history
   - Start the development server
   - Verify basic functionality
   - Choose and implement one feature
   - Test thoroughly
   - Commit changes and update progress file

## Key Principles

### Incremental Progress
- Work on one feature at a time
- Make small, verifiable changes
- Leave environment in a clean state

### Testing
- Always test end-to-end before marking features as complete
- Test as a human user would
- Use browser automation for web applications

### Documentation
- Commit changes with descriptive messages
- **Append** to `current_progress.txt` at the end of every session
  (never overwrite; the file is a history, not a snapshot). The
  legacy note: the framework used to ship a separate Initializer-only progress seed; it has been merged into `current_progress.txt` as of this revision
- Maintain clear git history

### Error Recovery
- Use git to revert bad changes
- Always verify basic functionality before implementing new features
- Fix existing bugs before adding new features

## Common Agent Failure Modes and Solutions

| Problem | Initializer Solution | Coding Agent Solution |
|---------|---------------------|---------------------|
| Declares victory too early | Set up feature list with all requirements | Read feature list, work on one feature at a time |
| Leaves environment broken | Set up git repo and progress notes | Read progress notes, test before new features |
| Marks features done prematurely | Set up feature list | Self-verify thoroughly before marking passing |
| Wastes time figuring out how to run app | Write `init.sh` script | Read `init.sh` at session start |

## References

- [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) - Anthropic Engineering Blog
- [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk) - SDK for building agents

## License

MIT
