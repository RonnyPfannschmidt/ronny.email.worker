"""mailmindctl."""

from __future__ import annotations

import json
from pathlib import Path

import attrs
import click
import sqlalchemy as sa

from mailmind.config import ConfigError, load_config
from mailmind.db import models as m
from mailmind.db.migrate import upgrade_to_head
from mailmind.service import TENANT_ZERO, Service, hash_token, mint_token


def _service(config_path: str | None, **overrides: object) -> Service:
    config = load_config(Path(config_path)) if config_path else load_config()
    if overrides:
        config = attrs.evolve(config, **overrides)
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
    "  See docs/12-an-agent-of-your-own.md."
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
def bootstrap(ctx: click.Context) -> None:
    """Create tenant zero's accounts from configuration, and a token for an agent."""
    service = _service(ctx.obj["config_path"])
    upgrade_to_head(service.config.database_url)

    with service.scope(TENANT_ZERO) as scope:
        for account_config in service.config.accounts:
            account = scope.scalar(
                sa.select(m.Account).where(m.Account.name == account_config.name)
            )
            if account is None:
                account = m.Account(
                    name=account_config.name,
                    host=account_config.host,
                    port=account_config.port,
                    use_ssl=account_config.use_ssl,
                    username=account_config.login.username,
                    password_url=account_config.login.password,
                    cache_bodies=account_config.cache_bodies,
                )
                scope.add(account)
                scope.flush()
                click.echo(f"created account {account.name}")
            for cap in account_config.caps:
                if not scope.scalar(
                    sa.select(m.AccountCapability.id).where(
                        m.AccountCapability.account_id == account.id,
                        m.AccountCapability.name == cap,
                    )
                ):
                    scope.add(m.AccountCapability(account_id=account.id, name=cap))
        scope.commit()
    click.echo("bootstrapped tenant zero")


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
@click.option("--account", "account_names", multiple=True, help="default: all accounts")
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


@main.command()
@click.option("--account", "account_name", default=None)
@click.pass_context
def sync(ctx: click.Context, account_name: str | None) -> None:
    """Bring the local cache into step with the mailboxes."""
    from mailmind.imap import sync as sync_module

    service = _service(ctx.obj["config_path"])
    with service.scope(TENANT_ZERO) as scope:
        stmt = sa.select(m.Account)
        if account_name:
            stmt = stmt.where(m.Account.name == account_name)
        for account in scope.scalars(stmt):
            with service.backend(account) as backend:
                for container in sync_module.discover_containers(scope, account, backend):
                    if not container.selectable:
                        continue
                    report = sync_module.sync_container(scope, account, container, backend)
                    if report.identity_broken:
                        click.secho(
                            f"{container.name}: RECREATED — {report.suggestions_killed} "
                            "suggestion(s) died with it",
                            fg="red",
                        )
                    elif report.added or report.updated or report.vanished:
                        click.echo(
                            f"{container.name}: +{report.added} ~{report.updated} "
                            f"-{report.vanished}"
                        )
                    # Per folder, not per account.  A first sync of a real mailbox is
                    # long, and one transaction around the whole of it holds SQLite's
                    # write lock for the duration — which every other request then waits
                    # on and gives up.  It also made the whole sync all-or-nothing, so
                    # interrupting an hour of fetching threw the hour away.
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
    leave_the_link(service.config.port, link)
    # Printed here, unlike the stdio mode: this command is one a person runs in their own
    # terminal, and its output is not collected into anybody's agent log.
    click.echo(f"review UI  {link}")
    click.echo("           open that link once — it is the login, and nothing")
    click.echo("           connecting over MCP is given it.")
    click.echo(LOCAL_WARNING)
    # The trailing slash is not decoration: the endpoint is mounted at /mcp/ and a POST to
    # /mcp gets a 307, which an MCP client is entitled to follow and may not.
    click.echo(f"MCP        {where}/mcp/")
    uvicorn.run(app, host=service.config.bind, port=service.config.port)


if __name__ == "__main__":
    main()
