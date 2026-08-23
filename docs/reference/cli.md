# `mailmindctl`

Rendered from the command itself. `--config` names the configuration file, as does
`MAILMIND_CONFIG`; [Configuration](configuration.md) has the rest of the file.

`sync` shows stacked progress at a terminal — the folders, the folder being read, and
messages against the total discovered so far — and prints a line per changed folder plus a
closing count when its output is going somewhere else.

::: mkdocs-click
    :module: mailmind.cli
    :command: main
    :prog_name: mailmindctl
    :depth: 1
    :style: table
