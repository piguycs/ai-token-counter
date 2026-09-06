# agent-usage

A small, dependency-free inline TUI that reads local agent usage data and shows total input, cached input, and output tokens, with a per-model breakdown. Codex and OpenCode are currently included. The UI does not use the terminal's alternate screen, so the final report remains visible after exit.

Run it:

```bash
just run
```

To build a single executable file you can move onto your `PATH`:

```bash
just build
mv dist/agent-usage ~/.local/bin/
agent-usage
```

The built file is a Python zipapp: it includes this project and needs Python 3.11+ on the machine where you run it. It does not install anything into the system Python environment.

The default view combines every model across all agents. Press `s` in that view to split the table by agent and model. Use `Tab` to switch between the combined view, Codex, and OpenCode. Left and right move between today, 7 days, 30 days, and all time.

Press `m` to enter calendar-month mode. Left and right then browse complete months, always using the first through the final day of that month; press `m` again to return. Press `c` for arbitrary custom dates or `q` to quit. Exact start and end dates are shown in the header.

For scripts or a one-shot report:

```bash
just report --period 30d
just report --since 2026-08-01 --until 2026-08-31
just opencode
just codex
```

For structured data, press `j` in the TUI to print JSON for the current agent and period. It includes input, cached input, uncached input, and output totals and per-agent/model rows. The equivalent script-friendly option is:

```bash
just report --json --period 30d
```

`--until` is inclusive when it is a date. Codex defaults to `$CODEX_HOME` or `~/.codex`; OpenCode defaults to `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`. Override either with `--data-dir PATH` (`--codex-home` remains an alias).

The parser supports both current `token_usage_record` entries and older `token_count` entries. When both occur in one session file, it uses the current records to avoid double-counting. Cached input is shown separately but is already part of the input-token count. For API-rate pricing, calculate regular input as `input − cached input`, then apply the model's regular-input, cached-input, and output rates to those three buckets.

New agents can be added by implementing the `UsageProvider` interface and registering the provider in `PROVIDERS`; period filtering, aggregation, CLI handling, and rendering are shared.

The shared display is intentionally limited to comparable model-usage metrics supported by every registered provider. OpenCode stores cached input outside its raw input count and reasoning outside its raw output count, so the provider normalizes both: input includes cached input and output includes reasoning tokens.
