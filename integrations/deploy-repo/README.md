# A deploy repository

The setup one machine runs mailmind with, kept apart from mailmind itself: this
`pyproject.toml` pulls mailmind in, `mailmind.service` runs it, and whatever the agent
needs (its instructions, its client configuration) sits beside them. The mailmind
configuration and database stay out of git, under `~/.config/mailmind/` and
`~/.local/share/mailmind/`.

## Setting up

```
cp -r ../ronny.email.worker/integrations/deploy-repo ~/src/my-mailmind && cd ~/src/my-mailmind
uv sync --frozen        # after the first `uv lock`
uv run mailmindctl migrate && uv run mailmindctl account seed && uv run mailmindctl sync
ln -s $PWD/mailmind.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now mailmind
```

## Redeploying

From this directory, with the configuration the unit uses:

```
MAILMIND_CONFIG=~/.config/mailmind/mailmind.toml uv run mailmindctl redeploy --unit mailmind
```

It waits until nothing is queued or running, stops the unit, builds the interface if
mailmind is a checkout, migrates, starts it again and checks it answers. To change the code
only while the unit is down, hand the change over:

```
uv run mailmindctl redeploy --unit mailmind \
    --while-stopped 'git -C ../ronny.email.worker pull --ff-only'
```

A failure after the stop leaves the unit stopped and says which step failed.
