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
