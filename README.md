# ZenLig AI Shop

A standalone Flask shop for `zenligai.online`. It reads the existing bot's SQLite database, QCST catalog and order API, Telegram identity, top-up methods, and wallet. The application does not create a replacement database: `DB_PATH` must be an absolute path to an existing shop database.

## Features

- Email/password accounts and Telegram Login Widget authentication.
- Link an email account to Telegram and merge its wallet and supported transaction history into the bot's Telegram wallet.
- QCST product listing and ordering with the bot's markup, stock limits, test-product visibility, idempotency key, atomic balance deduction, and refund handling.
- Order history and delivery data shared with the Telegram bot.
- Bank-transfer QR, USDT/TRC20 TXID submission, and prepaid cards through PPay.
- Admin top-up review; balances are credited only after an atomic approval.

Email-only accounts have their own web wallet until linked. Top-up review remains manual; submitting a payment notice or TXID never credits a balance by itself.

## Configuration

Copy `.env.example` to a protected runtime environment file outside source control. Fill the required values from the rotated credentials and current bot configuration. Do not put secrets in the source tree.

`DB_PATH` must point to the **same existing file** used by the Telegram bot (the current VPS path supplied for this deployment is `/root/mybot/shop.db`). The app refuses relative paths or missing database files.

**PPay card topup is optional.** To enable it, provide `PPAY_PARTNER_ID`, `PPAY_PARTNER_KEY`, and `DOITHE_CARD_ENCRYPTION_KEY`. The encryption key must match the key configured for the bot's PPay callback. When any of these three values are missing or empty, the card topup form is hidden and only bank transfer and USDT methods are available.

The Telegram bot username and website domain must also be configured for the Telegram Login Widget in BotFather.

The app adds `web_accounts` and the `orders.delivery` / `orders.source` columns on startup. Back up the existing shop database before first launch. Old bot orders default to `source='telegram'`.

## Local checks

Create a disposable copy of a real shop database for local testing; do not point a development process at production data:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

The service needs the variables above and an existing database before it starts. For production behind Caddy, run one process so there is one order-tracker worker:

```sh
waitress-serve --call --host=127.0.0.1 --port=8080 app:create_app
```

Configure the root domain to proxy to `127.0.0.1:8080`. Keep the existing `callback.zenligai.online` route to the bot's PPay callback listener unchanged.

## Deployment notes

This folder is a new standalone application; it has not been deployed to the VPS and does not change DNS or Caddy. The bot source must also be updated so its order tracker persists delivery content and handles top-up approval atomically. Restarting or replacing the current website is intentionally outside this local change.
