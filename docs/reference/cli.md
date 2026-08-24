# `mailmindctl`

Rendered from the command itself. `--config` names the configuration file, as does
`MAILMIND_CONFIG`; [Configuration](configuration.md) has the rest of the file.

Two commands make a database ready, and they are separate on purpose: `migrate` is the
only one that may run against a database older than itself, and it is the one that can
rewrite what is cached, so nothing else does it on the way past. `account seed` is the
other half — the configuration is seed data, the row is what a connection is built from,
and where they disagree it says so rather than deciding.

`sync` shows stacked progress at a terminal — the folders, the folder being read, and the
messages. The message total is real when it can be: a folder about to be read end to end is
counted with STATUS before anything is read, and one about to be asked what changed has no
knowable total, so it gets none rather than a made-up one. Output going somewhere else gets
a line per changed folder and a closing count instead.

::: mkdocs-click
    :module: mailmind.cli
    :command: main
    :prog_name: mailmindctl
    :depth: 1
    :style: table
