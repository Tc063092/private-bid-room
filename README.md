# Private Bid Room — working MVP

This is a real multi-member website using Python's standard library and SQLite. It is **not hosted** yet.

## Run locally

Requires Python 3.10+ (no packages to install).

macOS / Linux:

```bash
cd private_auction
export AUCTION_ADMIN_PASSWORD='replace-with-a-strong-unique-password'
python3 server.py
```

Windows PowerShell:

```powershell
cd private_auction
$env:AUCTION_ADMIN_PASSWORD='replace-with-a-strong-unique-password'
python server.py
```

Open http://localhost:8000. The SQLite database is created in the current directory.

## Make it available to friends

Deploy to a host that supports a long-running Python HTTP process **and persistent disk** (SQLite must survive restarts). Set `AUCTION_ADMIN_PASSWORD`, `AUCTION_DB` (persistent path), and `PORT` (host-assigned port). Put the app behind HTTPS and a reverse proxy. Share the deployed URL via WhatsApp. Avoid ephemeral-disk deployments because bids would be lost.

## Rules

- Members register/login with unique name and PIN; one active bid per member.
- Members can raise but not lower their bid. Each new bid must exceed their previous amount by the organizer-configured increment. First bid must meet the starting price.
- Members choose public or hidden for each new bid. The organizer can see all amounts.
- Organizer configures starting price, increment, title, and optional closing time; can close early.
- After closing, all visitors see only the winning member and winning amount (plus previously public bids). Hidden losing bids remain hidden.
- In a tie, earliest timestamp of the *current winning amount* wins; in the rare same-second case, lower bid record ID wins.

## Security / production caveats

This is a functional MVP, not a security-audited production service. Use HTTPS, a strong organizer password, database backups, a persistent volume, rate limiting and account recovery before inviting untrusted users. Usernames are self-asserted (no email/phone verification). Server-side rules prevent ordinary members from requesting hidden amounts through the API. Organizer sessions last 14 days. The host's HTTPS reverse proxy should set `X-Forwarded-Proto: https` so cookies get the Secure flag. Do not use this as a regulated financial auction platform without a legal/security review.

## Organizer closing time

Log in as organizer, open Auction settings, choose **Automatic closing date/time** in your browser local timezone (Malaysia: UTC+8), and click **Save settings**. The bidding board displays the closing date and a live countdown. The server rejects new or increased bids at or after the deadline, and the winning name and price appear automatically. Leave the date blank to close manually.

## Organizer approval (new)

New registrations start in **Pending** status. The organizer signs in and uses **Member approval requests** to approve or reject each account. Pending members can log in and see the board but cannot bid. Rejected pending accounts are deleted and their sessions revoked. Existing members from older database versions are also set to pending on migration and must be approved.

## Deploy with Render (persistent storage)

1. Upload the project files to a **private GitHub repository** (keep passwords and database files out of Git).
2. In Render, create a **Blueprint** from that repository; `render.yaml` configures a Docker web service with a persistent 1 GB disk. A paid service plan may be required for persistent disks; confirm current Render pricing before deployment.
3. When prompted, enter a strong, unique `AUCTION_ADMIN_PASSWORD` as a secret environment variable.
4. Deploy, wait for the service to become healthy, and open its HTTPS URL.
5. Test registration, pending approval, organizer approval, hidden bids, raising bids and scheduled closing using two different browsers before inviting real members.
6. Share the HTTPS link using the site's **Share link** button.

**Important:** This package does not create an online account, purchase hosting, deploy the service, configure backups, or provide a live URL on its own. Back up `/data/auction.sqlite3` regularly. This MVP is intended for trusted friends, not public high-stakes auctions. For public use, add independent security review, authentication hardening and operational monitoring.
