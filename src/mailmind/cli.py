"""mailmindctl."""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import attrs
import click
import sqlalchemy as sa

from mailmind.config import ConfigError, load_config
from mailmind.db import models as m
from mailmind.db.migrate import (
    SchemaBehind,
    current_revision,
    require_current_schema,
    upgrade_to_head,
)
from mailmind.service import TENANT_ZERO, Service, hash_token, mint_token


def _service(
    config_path: str | None, *, needs_schema: bool = True, **overrides: object
) -> Service:
    """The service, and a refusal to work against a database this build does not match.

    Every command but `migrate` asks for the schema it was written against. Without
    that, a checkout that has moved on meets the old schema at the first query and says
    `no such column` from somewhere in the middle of a sync.
    """
    config = load_config(Path(config_path)) if config_path else load_config()
    if overrides:
        config = attrs.evolve(config, **overrides)
    if needs_schema:
        try:
            require_current_schema(config.database_url)
        except SchemaBehind as exc:
            raise click.ClickException(str(exc)) from exc
    return Service(config)


@click.group()
@click.option("--config", "config_path", default=None, help="path to mailmind.toml")
@click.pass_context
def main(ctx: click.Context, config_path: str | None) -> None:
    """Browse a mailbox with an agent; decide what happens to it yourself."""
    ctx.obj = {"config_path": config_path}


#: Said out loud wherever a review UI comes up, because the arrangement is easy to
#: mistake for a stronger one than it is.
LOCAL_WARNING = (
    "\n  This is a local deployment. Its boundary is that the review key is never given\n"
    "  to the agent — not that the agent cannot reach it. Anything running as you can\n"
    "  read that key, and read the mailbox password out of your configuration without\n"
    "  going near mailmind at all. If that matters, sandbox the agent.\n"
    "  See docs/security-model.md."
)


@main.command()
@click.option("--open", "open_it", is_flag=True, help="follow the link in a browser")
@click.option("--port", default=None, type=int, help="which server, if several are up")
@click.pass_context
def review(ctx: click.Context, open_it: bool, port: int | None) -> None:
    """Print the link that opens the review UI, or follow it.

    The link carries the key, which is why it is left in a file rather than printed by
    anything an MCP client collects.
    """
    from mailmind.web.app import link_path

    service = _service(ctx.obj["config_path"])
    path = link_path(port if port is not None else service.config.port)
    if not path.exists():
        raise click.ClickException(
            f"no review UI has left a link at {path} — start one with `mailmindctl serve`, "
            "or name its port with --port"
        )
    link = path.read_text().strip()
    if open_it:
        import webbrowser

        webbrowser.open(link)
        click.echo(f"opened {link.split('?')[0]}")
        return
    click.echo(link)


@main.command()
@click.pass_context
def migrate(ctx: click.Context) -> None:
    """Bring the database up to the schema this build was written against.

    The only command that may meet a database older than itself; every other one refuses
    and says to run this. Which is the split: a migration can rewrite what is cached — 0004
    does — and that is not something a command whose job is something else should do on the
    way past.
    """
    service = _service(ctx.obj["config_path"], needs_schema=False)
    url = service.config.database_url
    was = current_revision(url)
    upgrade_to_head(url)
    now = current_revision(url)
    if was == now:
        click.echo(f"already at {now}")
    else:
        click.echo(f"{was or 'empty database'} → {now}")
    service.close()


#: What a configured account and its row have to say to each other. The row is the source
#: of truth once it exists, so a difference is reported rather than resolved — unless
#: somebody says which way it should go.
SEEDED_FIELDS = (
    ("host", lambda config: config.host),
    ("port", lambda config: config.port),
    ("use_ssl", lambda config: config.use_ssl),
    ("username", lambda config: config.login.username),
    ("password_url", lambda config: config.login.password),
    ("cache_bodies", lambda config: config.cache_bodies),
)


@main.command("grant")
@click.option("--producer", default="opencode", help="name of the agent this is for")
@click.option(
    "--capability",
    "capabilities",
    multiple=True,
    default=("observe", "suggest", "assess"),
    type=click.Choice([c.value for c in m.Capability]),
    help="there is no apply; it is not a capability this service has",
)
@click.option(
    "--account", "account_names", multiple=True, default=(), help="default: all accounts"
)
@click.pass_context
def make_grant(
    ctx: click.Context,
    producer: str,
    capabilities: tuple[str, ...],
    account_names: tuple[str, ...],
) -> None:
    """Mint a bearer token. Printed once; only its hash is stored."""
    service = _service(ctx.obj["config_path"])
    token = mint_token()
    with service.scope(TENANT_ZERO) as scope:
        row = scope.scalar(sa.select(m.Producer).where(m.Producer.name == producer))
        if row is None:
            row = m.Producer(kind=m.ProducerKind.agent, name=producer)
            scope.add(row)
            scope.flush()
        grant = m.Grant(
            producer_id=row.id, token_hash=hash_token(token), capabilities=list(capabilities)
        )
        scope.add(grant)
        scope.flush()
        accounts = scope.scalars(
            sa.select(m.Account).where(m.Account.name.in_(account_names))
            if account_names
            else sa.select(m.Account)
        ).all()
        for account in accounts:
            scope.add(m.GrantAccount(grant_id=grant.id, account_id=account.id))
        scope.audit(
            "grant_minted",
            actor_kind="person",
            subject_kind="grant",
            subject_id=grant.id,
            payload={"producer": producer, "capabilities": list(capabilities)},
        )
        scope.commit()
        click.echo(f"grant {grant.id} for {producer} over {len(accounts)} account(s)")
    click.echo(f"\n  {token}\n")
    click.echo("This is shown once. Give it to the agent as a bearer token.")


@main.command()
@click.pass_context
def probe(ctx: click.Context) -> None:
    """Check every account's declared capabilities against the server. Loud on divergence."""
    from mailmind.imap.capabilities import probe_account

    service = _service(ctx.obj["config_path"])
    diverged = False
    with service.scope(TENANT_ZERO) as scope:
        for account in scope.scalars(sa.select(m.Account)):
            with service.backend(account) as backend:
                report = probe_account(scope, account, backend)
            if report.missing:
                diverged = True
                click.secho(
                    f"{account.name}: DECLARED BUT NOT OFFERED: {', '.join(report.missing)}",
                    fg="red",
                )
            if report.undeclared:
                click.echo(
                    f"{account.name}: offered but not declared: {', '.join(report.undeclared)}"
                )
            if not report.diverged:
                click.secho(f"{account.name}: as declared", fg="green")
        scope.commit()
    raise SystemExit(1 if diverged else 0)


@contextlib.contextmanager
def sync_display(folders: int):  # noqa: ANN201
    """Show where a sync has got to, if there is anybody watching.

    A first sync is 187 folders and tens of thousands of messages, and used to print one
    line per folder *after* that folder finished — so the interesting part, the long one,
    was silent. Three bars: the folders, the folder being read, and the messages seen so
    far, which has no total because finding one out means selecting every folder first.

    Redirected or run from a unit there is nobody watching a bar, so it falls back to the
    line per folder that a log wants: the same decision `serve` makes about its link.
    """
    if not sys.stdout.isatty():
        lines = _Lines()
        try:
            yield lines
        finally:
            lines.finish()
        return

    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        transient=False,
    ) as progress:
        yield _Bars(progress, folders)


class _Lines:
    """One line per folder that changed, and a count at the end. For logs and pipes."""

    def __init__(self) -> None:
        self.folders = 0
        self.messages = 0

    def folder_started(self, container: str, messages: int) -> None:
        self.folders += 1

    def messages_absorbed(self, count: int) -> None:
        self.messages += count

    def finish(self) -> None:
        # Something, even when nothing changed: a unit whose log says nothing at all is a
        # unit nobody can tell apart from one that never ran.
        click.echo(f"{self.folders} folder(s), {self.messages} message(s) read")

    def folder_finished(self, report) -> None:  # noqa: ANN001
        if report.identity_broken:
            click.secho(
                f"{report.container}: RECREATED — {report.suggestions_killed} "
                "suggestion(s) died with it",
                fg="red",
            )
        elif report.added or report.updated or report.vanished:
            click.echo(
                f"{report.container}: +{report.added} ~{report.updated} -{report.vanished}"
            )


class _Bars:
    """The same three facts, stacked, for somebody watching it happen."""

    def __init__(self, progress, folders: int) -> None:  # noqa: ANN001
        self._progress = progress
        self._folders = progress.add_task("folders", total=folders)
        self._folder = progress.add_task("waiting", total=None)
        self._messages = progress.add_task("messages", total=None)
        #: Grows as folders are opened. The alternative is knowing the whole mailbox up
        #: front, which means selecting all 187 folders before reading any of them.
        self._known = 0

    def folder_started(self, container: str, messages: int) -> None:
        self._known += messages
        self._progress.reset(self._folder, total=messages, description=container)
        self._progress.update(self._messages, total=self._known)

    def messages_absorbed(self, count: int) -> None:
        self._progress.advance(self._folder, count)
        self._progress.advance(self._messages, count)

    def folder_finished(self, report) -> None:  # noqa: ANN001
        self._progress.advance(self._folders)
        if report.identity_broken:
            self._progress.console.print(
                f"[red]{report.container}: RECREATED — {report.suggestions_killed} "
                "suggestion(s) died with it[/red]"
            )
        elif report.added or report.updated or report.vanished:
            self._progress.console.print(
                f"{report.container}: +{report.added} ~{report.updated} -{report.vanished}"
            )


@main.command()
@click.option("--account", "account_name", default=None)
@click.option(
    "--full",
    "force_full",
    is_flag=True,
    help="re-read every message rather than what changed, which is how a cache filled by "
    "an older parse gets corrected",
)
@click.pass_context
def sync(ctx: click.Context, account_name: str | None, force_full: bool) -> None:
    """Bring the local cache into step with the mailboxes.

    Incremental by default: a folder is asked what changed since last time.  ``--full``
    asks for all of it, which is what to do after this program learns to read something it
    was reading wrongly — the cache holds what the parser made of a message, not the
    message.
    """
    from mailmind.imap import sync as sync_module

    service = _service(ctx.obj["config_path"])
    with service.scope(TENANT_ZERO) as scope:
        stmt = sa.select(m.Account)
        if account_name:
            stmt = stmt.where(m.Account.name == account_name)
        for account in scope.scalars(stmt):
            with service.backend(account) as backend:
                folders = [
                    c
                    for c in sync_module.discover_containers(scope, account, backend)
                    if c.selectable
                ]
                with sync_display(len(folders)) as display:
                    for container in folders:
                        report = sync_module.sync_container(
                            scope,
                            account,
                            container,
                            backend,
                            force_full=force_full,
                            progress=display,
                        )
                        display.folder_finished(report)
                        # Per folder, not per account.  A first sync of a real mailbox is
                        # long, and one transaction around the whole of it holds SQLite's
                        # write lock for the duration — which every other request then
                        # waits on and gives up.  It also made the whole sync
                        # all-or-nothing, so interrupting an hour of fetching threw the
                        # hour away.
                        scope.commit()
    service.close()


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """What is waiting for a person."""
    from mailmind import views

    service = _service(ctx.obj["config_path"])
    with service.scope(TENANT_ZERO) as scope:
        bundles = views.bundle_summaries(scope, [m.BundleStatus.proposed])
        click.echo(json.dumps(bundles, indent=2))


@main.command("mcp")
@click.option(
    "--producer",
    default="local",
    help="whose proposals these are, in the record. Reuses that producer's grant if it "
    "has one, so `grant --producer NAME --capability observe` narrows this too.",
)
@click.option(
    "--token",
    default=None,
    envvar="MAILMIND_TOKEN",
    help="use an existing grant token instead, exactly as the HTTP endpoint would",
)
@click.option(
    "--serve",
    "serve_ui",
    is_flag=True,
    help="bring the review UI up too, for as long as this session lasts",
)
@click.option(
    "--port",
    default=None,
    type=int,
    help="with --serve: which port to take. 0 picks a free one.",
)
@click.option(
    "--review-url",
    default=None,
    envvar="MAILMIND_REVIEW_URL",
    help="where the review UI already is, if not where this configuration says",
)
@click.pass_context
def mcp_stdio(
    ctx: click.Context,
    producer: str,
    token: str | None,
    serve_ui: bool,
    port: int | None,
    review_url: str | None,
) -> None:
    """Speak MCP on stdin and stdout, and say where the review is.

    This is the shape an MCP client expects: it spawns the process and talks down a pipe,
    so there is no port to configure and no token to paste.

    Either way the model is told where the review UI is — in the instructions at connect
    time and in the note on every bundle — so the agent can tell whoever it is working for
    where to go.  What ``--serve`` decides is whether that UI is this process or another
    one.

    Without it, the address comes from the configuration and is expected to be a
    ``mailmindctl serve`` that outlives any one session.  With it, this process brings the
    UI up itself and takes it down again at the end, which is the whole of the setup for
    somebody whose agent is the only thing that ever proposes anything: start the agent,
    get told where to review, review it while the agent is still there.
    """
    import asyncio
    import socket

    import uvicorn

    from mailmind.mcp import server as mcp_server
    from mailmind.web import app as web_app
    from mailmind.web.app import create_app, mint_session_key

    if review_url and serve_ui:
        raise click.ClickException(
            "--review-url says where the UI already is and --serve brings one up; "
            "one or the other"
        )
    if port is not None and not serve_ui:
        raise click.ClickException("--port only means anything with --serve")

    overrides = {"port": port} if port is not None else {}
    service = _service(ctx.obj["config_path"], **overrides)

    # Minted only when this process is the one serving. The model is told the address
    # and never this: an agent can send a person to the review UI and cannot go itself.
    session_key = mint_session_key() if serve_ui else None

    listener: socket.socket | None = None
    if serve_ui:
        bind = service.config.bind
        family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        try:
            listener = socket.create_server(
                (bind, service.config.port), family=family, backlog=128
            )
        except OSError as exc:
            raise click.ClickException(
                f"cannot serve the review UI on {bind}:{service.config.port}: {exc}. "
                "If something is already serving there, drop --serve and it will be "
                "advertised instead; otherwise --port 0 takes a free one."
            ) from exc
        # Bound before the server is built, because instructions are fixed at construction
        # and the address has to be in them — which is also what makes --port 0 workable.
        shown = f"[{bind}]" if ":" in bind else bind
        review_url = f"http://{shown}:{listener.getsockname()[1]}/"
    elif review_url is None:
        bind = service.config.bind
        shown = f"[{bind}]" if ":" in bind else bind
        review_url = f"http://{shown}:{service.config.port}/"
    if not review_url.endswith("/"):
        review_url += "/"

    try:
        context = (
            mcp_server.grant_context(service, token)
            if token
            else mcp_server.local_context(service, producer)
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if context is None:
        raise click.ClickException("that token does not resolve to a grant that is still live")

    server = mcp_server.build_server(service, review_url=review_url)

    async def run() -> None:
        mcp_server.CURRENT_GRANT.set(context)
        if listener is None:
            await server.run_stdio_async()
            return
        # No MCP endpoint on it: the agent is already on the pipe, and mounting a second
        # way in would be surface nobody asked for.
        web = uvicorn.Server(
            uvicorn.Config(
                create_app(service, with_mcp=False, session_key=session_key),
                log_level="warning",
                access_log=False,
            )
        )
        serving = asyncio.create_task(web.serve(sockets=[listener]))
        try:
            await server.run_stdio_async()
        finally:
            web.should_exit = True
            await asyncio.gather(serving, return_exceptions=True)

    # stdout is the transport, so everything a person reads goes to stderr.
    click.echo(f"mailmind MCP on stdio as {producer!r}", err=True)
    if serve_ui:
        # The key goes to a file and never to stderr: an MCP client collects the stderr of
        # what it spawns into a log, and some put that log in front of the model.
        left_at = web_app.leave_the_link(
            listener.getsockname()[1], f"{review_url}?key={session_key}"
        )
        click.echo(f"review UI  {review_url}  (this session only)", err=True)
        click.echo(f"           the link that opens it is in {left_at}", err=True)
        click.echo("           `mailmindctl review --open` follows it for you", err=True)
        click.echo(LOCAL_WARNING, err=True)
    else:
        click.echo(f"review UI  {review_url} — `mailmindctl serve` runs it", err=True)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover - the client closing the pipe
        pass
    finally:
        service.close()


@main.group()
def account() -> None:
    """The accounts this mailmind knows about.

    Adding one belongs in the review UI, because the ``account`` row is the source of
    truth and the configuration is seed data for it.  Undoing a *seed* does not: a copy
    of the example file, seeded once, leaves an account behind that nothing else here can
    remove and that the review UI goes on offering.
    """


@account.command("list")
@click.pass_context
def list_accounts(ctx: click.Context) -> None:
    """What is in the database, and whether the configuration still asks for it."""
    service = _service(ctx.obj["config_path"])
    configured = {a.name for a in service.config.accounts}
    with service.scope(TENANT_ZERO) as scope:
        chosen = {
            p.current_account_id
            for p in scope.scalars(sa.select(m.Producer))
            if p.current_account_id
        }
        for row in scope.scalars(sa.select(m.Account).order_by(m.Account.name)):
            folders = scope.scalar(
                sa.select(sa.func.count())
                .select_from(m.Container)
                .where(m.Container.account_id == row.id)
            )
            notes = []
            if row.name not in configured:
                notes.append("not in the configuration")
            if row.id in chosen:
                notes.append("being reviewed")
            trailer = f"  ({', '.join(notes)})" if notes else ""
            # Not `user@host`: a username is usually an address already, and two @ in a
            # row reads as a typo.
            click.echo(
                f"{row.name}  {row.username} on {row.host}:{row.port}  "
                f"{row.health.value}  {folders} folder(s){trailer}"
            )


@account.command("seed")
@click.option(
    "--update",
    "apply_changes",
    is_flag=True,
    help="write the configuration's values over the row's, for the accounts that differ",
)
@click.pass_context
def seed_accounts(ctx: click.Context, apply_changes: bool) -> None:
    """Create the account rows the configuration names and the database does not have.

    Seed data, which is what the configuration is: a connection is built from the row.
    That is why a change to the file does not reach a running account by itself — and why
    this reports the difference rather than passing over it, which is how an account went
    on connecting to a host its configuration had stopped naming.
    """
    service = _service(ctx.obj["config_path"])
    with service.scope(TENANT_ZERO) as scope:
        for account_config in service.config.accounts:
            row = scope.scalar(
                sa.select(m.Account).where(m.Account.name == account_config.name)
            )
            if row is None:
                row = m.Account(
                    name=account_config.name,
                    host=account_config.host,
                    port=account_config.port,
                    use_ssl=account_config.use_ssl,
                    username=account_config.login.username,
                    password_url=account_config.login.password,
                    cache_bodies=account_config.cache_bodies,
                )
                scope.add(row)
                scope.flush()
                click.echo(f"created account {row.name}")
            else:
                _reconcile(row, account_config, apply_changes=apply_changes)
            _reconcile_capabilities(scope, row, account_config, apply_changes=apply_changes)
        scope.commit()
    service.close()


def _reconcile(row, account_config, *, apply_changes: bool) -> None:  # noqa: ANN001
    for field, of_config in SEEDED_FIELDS:
        wanted = of_config(account_config)
        held = getattr(row, field)
        if held == wanted:
            continue
        if apply_changes:
            setattr(row, field, wanted)
            click.echo(f"{row.name}: {field} {held!r} → {wanted!r}")
        else:
            click.secho(
                f"{row.name}: {field} is {held!r}, the configuration says {wanted!r} "
                "— `--update` writes it",
                fg="yellow",
            )


def _reconcile_capabilities(scope, row, account_config, *, apply_changes: bool) -> None:  # noqa: ANN001
    """The declared half of the capability rows, which is not all of them.

    ``probe`` writes what a server offered into the same table with ``declared`` false, so
    a row is not a declaration — the flag on it is. Deleting rows the configuration does
    not name would throw away everything the last probe learned.
    """
    wanted = set(account_config.caps)
    held = {
        c.name: c
        for c in scope.scalars(
            sa.select(m.AccountCapability).where(m.AccountCapability.account_id == row.id)
        )
    }
    for name in sorted(wanted):
        capability = held.get(name)
        if capability is None:
            scope.add(m.AccountCapability(account_id=row.id, name=name, declared=True))
            click.echo(f"{row.name}: declares {name}")
        elif not capability.declared:
            capability.declared = True
            click.echo(f"{row.name}: declares {name}, which was only ever offered")
    for name, capability in sorted(held.items()):
        # A declaration decides what the service attempts, so one the file has dropped is
        # a claim nobody is making any more. The row stays: the probe's half of it is a
        # fact about the server, not about the configuration.
        if name in wanted or not capability.declared:
            continue
        if apply_changes:
            capability.declared = False
            click.echo(f"{row.name}: no longer declares {name}")
        else:
            click.secho(
                f"{row.name}: still declares {name}, which the configuration does not "
                "— `--update` withdraws that",
                fg="yellow",
            )


@account.command("forget")
@click.argument("name")
@click.pass_context
def forget_account(ctx: click.Context, name: str) -> None:
    """Remove an account this mailmind should never have had.

    Only one it holds no mail for: there is no cascade behind this and dropping folders
    and messages out from under a cache is not something to do in passing.
    """
    service = _service(ctx.obj["config_path"])
    if any(a.name == name for a in service.config.accounts):
        raise click.ClickException(
            f"{name!r} is still named in the configuration, so `account seed` would put "
            "it back — take it out of the file first"
        )
    with service.scope(TENANT_ZERO) as scope:
        row = scope.scalar(sa.select(m.Account).where(m.Account.name == name))
        if row is None:
            raise click.ClickException(f"no account named {name!r}")
        folders = scope.scalar(
            sa.select(sa.func.count())
            .select_from(m.Container)
            .where(m.Container.account_id == row.id)
        )
        if folders:
            raise click.ClickException(
                f"{name!r} holds {folders} cached folder(s); forgetting it would leave "
                "them behind, and nothing here removes them yet"
            )
        for producer in scope.scalars(
            sa.select(m.Producer).where(m.Producer.current_account_id == row.id)
        ):
            # The preference goes, the producer stays: it is what "who accepted this"
            # points at.
            producer.current_account_id = None
        for link in scope.scalars(
            sa.select(m.GrantAccount).where(m.GrantAccount.account_id == row.id)
        ):
            scope.delete(link)
        scope.audit(
            "account_forgotten",
            actor_kind="person",
            subject_kind="account",
            subject_id=row.id,
            payload={"name": row.name, "host": row.host},
        )
        scope.delete(row)
        scope.commit()
    click.echo(f"forgot {name}")


@main.command()
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Run the review UI and the MCP endpoint."""
    import uvicorn

    from mailmind.web.app import create_app, mint_session_key

    # An override has to reach the configuration rather than only uvicorn: the MCP
    # endpoint builds its DNS-rebinding allow-list from the configured bind address, so
    # a service told to listen elsewhere would refuse the very Host it was serving.
    overrides = {key: value for key, value in (("bind", host), ("port", port)) if value}
    service = _service(ctx.obj["config_path"], **overrides)
    session_key = mint_session_key()
    try:
        app = create_app(service, session_key=session_key)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    from mailmind.web.app import leave_the_link

    where = f"http://{service.config.bind}:{service.config.port}"
    link = f"{where}/?key={session_key}"
    left_at = leave_the_link(service.config.port, link)
    #: Is there a person reading this, or a log keeping it? The key goes to the first and
    #: never to the second.
    a_terminal = sys.stdout.isatty()
    if a_terminal:
        # Printed in full, unlike the stdio mode: this is a command a person runs in their
        # own terminal, and a terminal is not collected into anybody's log.
        click.echo(f"review UI  {link}")
        click.echo("           open that link once — it is the login, and nothing")
        click.echo("           connecting over MCP is given it.")
    else:
        # Under systemd or a redirect there is no person reading this, and whatever is
        # reading it keeps what it reads. So the key stays in the file it was already
        # written to, and what goes out is where to find it — on stderr, because a unit
        # that sends stdout to /dev/null is the ordinary way to run this.
        click.echo(f"review UI  {where}/", err=True)
        click.echo(f"           the link that opens it is in {left_at}", err=True)
        click.echo("           `mailmindctl review --open` follows it for you", err=True)
    click.echo(LOCAL_WARNING, err=not a_terminal)
    # The trailing slash is not decoration: the endpoint is mounted at /mcp/ and a POST to
    # /mcp gets a 307, which an MCP client is entitled to follow and may not.
    click.echo(f"MCP        {where}/mcp/", err=not a_terminal)
    uvicorn.run(app, host=service.config.bind, port=service.config.port)


if __name__ == "__main__":
    main()
