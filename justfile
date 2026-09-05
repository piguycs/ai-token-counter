default:
    @just --list

# Open the inline TUI.
run *args:
    python3 -m agent_usage {{args}}

# Print a report without opening the TUI.
report *args:
    python3 -m agent_usage --no-ui {{args}}

# Run the test suite without writing bytecode into the project.
test:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v

# Build a Python 3.11+ zipapp at dist/agent-usage for copying to PATH.
build:
    rm -rf .build/agent-usage
    mkdir -p .build/agent-usage dist
    cp -R agent_usage .build/agent-usage/
    cp codex_tokens.py .build/agent-usage/
    python3 -m zipapp .build/agent-usage -m 'agent_usage:main' -p '/usr/bin/env python3' -o dist/agent-usage
    chmod +x dist/agent-usage

# Open a report for one agent.
codex *args:
    python3 -m agent_usage --agent codex {{args}}

opencode *args:
    python3 -m agent_usage --agent opencode {{args}}
