"""Put the test corpus into a throwaway IMAP server, so there is something to look at.

This talks to IMAP directly rather than through :class:`~mailmind.imap.backend.MailBackend`,
and it lives outside the package on purpose.  04 puts writing mail outside the backend
surface — there is no append, and the one thing the shipped code may do to a mailbox is
apply a suggestion a person accepted.  A seeder that went through the protocol would have
to widen it, and then the shipped artifact would contain a way to write mail that nobody
reviewed.  So it does not, and this is a script in the repository rather than a
subcommand of ``mailmindctl``.

    podman run -d --rm --name mailmind-dev -p 3144:143 -e MAILNAME=example.org \
      -e MAIL_ADDRESS=me@example.org -e MAIL_PASS=secret \
      docker.io/antespi/docker-imap-devel:latest
    python dev/seed_mailbox.py

See docs/test-drive.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imapclient import IMAPClient  # noqa: E402

from tests.corpus import CORPUS  # noqa: E402

#: Somewhere for a move suggestion to point at.  Dovecot ships Sent, Trash and Drafts and
#: not this one, and a review UI with nowhere to file anything is not worth looking at.
EXTRA_FOLDERS = ("Archive",)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default="127.0.0.1:3144", help="host:port")
    parser.add_argument("--user", default="me@example.org")
    parser.add_argument("--password", default="secret")
    parser.add_argument("--ssl", action="store_true")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="empty INBOX first, rather than appending another copy of the corpus",
    )
    args = parser.parse_args(argv)

    host, _, port = args.target.partition(":")
    client = IMAPClient(host, port=int(port or 143), ssl=args.ssl)
    client.login(args.user, args.password)
    try:
        for folder in EXTRA_FOLDERS:
            if not client.folder_exists(folder):
                client.create_folder(folder)
                print(f"created {folder}")

        client.select_folder("INBOX", readonly=False)
        if args.reset:
            uids = client.search(["ALL"])
            if uids:
                client.delete_messages(uids)
                client.expunge()
                print(f"emptied INBOX ({len(uids)} message(s))")

        for name, raw in CORPUS.items():
            client.append("INBOX", raw)
            print(f"appended {name}")
    finally:
        client.logout()

    print(f"\n{len(CORPUS)} message(s) in INBOX on {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
