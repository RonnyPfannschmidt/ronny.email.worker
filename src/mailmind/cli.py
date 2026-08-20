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
@click.option("--port", default=None, type=int, help="review UI port; 0 picks a free one")
@click.option("--no-review-ui", is_flag=True, help="speak MCP and serve nothing")
@click.pass_context
def mcp_stdio(  # noqa: C901
    ctx: click.Context,
    producer: str,
    token: str | None,
    port: int | None,
    no_review_ui: bool,
) -> None:
    """Speak MCP on stdin and stdout, with the review UI on a local port.

    This is the shape an MCP client expects: it spawns the process and talks down a pipe,
    so there is no port to configure, no token to paste and nothing to start first.

    The review UI still needs somewhere to be, because a proposal nobody looks at is not a
    proposal. It comes up alongside, and its address is given to the model at connect time
    and repeated on every bundle — the agent can then say where to go and the person can
    go there.
    """
    import asyncio
    import socket

    import uvicorn

    from mailmind.mcp import server as mcp_server
    from mailmind.web.app import create_app

    overrides = {"port": port} if port is not None else {}
    service = _service(ctx.obj["config_path"], **overrides)

    # Nothing here writes to stdout: it is the transport, so every message goes to stderr.
    # The MCP SDK does defend this — stdio_server claims fd 1 and moves the real stdout to
    # a private descriptor, so a stray print cannot corrupt the protocol. That is a net
    # under the floor rather than a reason to walk about: uvicorn's access log is turned
    # off below because a log nobody can read is not worth writing, not because it would
    # otherwise land on the wire.
    def tell(message: str) -> None:
        click.echo(message, err=True)

    listener: socket.socket | None = None
    review_url: str | None = None
    if not no_review_ui:
        bind = service.config.bind
        family = socket.AF_INET6 if ":" in bind else socket.AF_INET
        try:
            listener = socket.create_server(
                (bind, service.config.port), family=family, backlog=128
            )
        except OSError as exc:
            raise click.ClickException(
                f"cannot serve the review UI on {bind}:{service.config.port}: {exc}. "
                "Use --port 0 for a free one, or --no-review-ui."
            ) from exc
        # Bound before the server is built, because the address has to be known in time to
        # go into the instructions the model is given at connect time.
        shown = f"[{bind}]" if ":" in bind else bind
        review_url = f"http://{shown}:{listener.getsockname()[1]}/"

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
        web = uvicorn.Server(
            uvicorn.Config(
                create_app(service, with_mcp=False),
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

    tell(f"mailmind MCP on stdio as {producer!r}")
    if review_url:
        tell(f"review UI  {review_url}")
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

    from mailmind.web.app import create_app

    # An override has to reach the configuration rather than only uvicorn: the MCP
    # endpoint builds its DNS-rebinding allow-list from the configured bind address, so
    # a service told to listen elsewhere would refuse the very Host it was serving.
    overrides = {key: value for key, value in (("bind", host), ("port", port)) if value}
    service = _service(ctx.obj["config_path"], **overrides)
    try:
        app = create_app(service)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    where = f"http://{service.config.bind}:{service.config.port}"
    # The trailing slash is not decoration: the endpoint is mounted at /mcp/ and a POST to
    # /mcp gets a 307, which an MCP client is entitled to follow and may not.
    click.echo(f"review UI  {where}/\nMCP        {where}/mcp/")
    uvicorn.run(app, host=service.config.bind, port=service.config.port)


if __name__ == "__main__":
    main()
