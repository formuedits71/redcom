# Load Confirmation Telegram Bot

A production-ready Telegram bot that reads a PDF rate confirmation / load
document, extracts the shipment data, calculates driving mileage, and uses
Google Gemini to produce a finished **Load Confirmation** in each user's own
saved template format. Supports English, Russian and Uzbek.

Everything lives in a single file: `main.py`.

> The bot uses Google's official `google-genai` SDK (`from google import genai`)
> and the `gemini-3.5-flash` model, calling Gemini asynchronously via
> `client.aio.models.generate_content(...)` for both native PDF understanding
> and template generation.

---

## Table of Contents

1. [How the bot works](#how-the-bot-works)
2. [Installing Python](#installing-python)
3. [Creating a Telegram Bot & getting a Bot Token](#creating-a-telegram-bot--getting-a-bot-token)
4. [Getting a Gemini API Key](#getting-a-gemini-api-key)
5. [Getting an OpenRouteService API Key](#getting-an-openrouteservice-api-key)
6. [Creating a Railway PostgreSQL database](#creating-a-railway-postgresql-database)
7. [Getting DATABASE_URL](#getting-database_url)
8. [Setting ADMIN_ID](#setting-admin_id)
9. [Creating your .env file](#creating-your-env-file)
10. [Installing dependencies](#installing-dependencies)
11. [Running locally](#running-locally)
12. [Deploying on Railway](#deploying-on-railway)
13. [Deploying on Render](#deploying-on-render)
14. [Environment variables reference](#environment-variables-reference)
15. [How the template system works](#how-the-template-system-works)
16. [How the cache works](#how-the-cache-works)
17. [How the admin panel works](#how-the-admin-panel-works)
18. [Available commands](#available-commands)
19. [Common errors & troubleshooting](#common-errors--troubleshooting)

---

## How the bot works

1. A user sends `/start`.
2. If they don't have a saved template yet, the bot asks them to send one
   (plain text). This is saved **forever** — once per user, in PostgreSQL,
   and mirrored in an in-RAM cache for fast access.
3. Once a template exists, the bot asks for a PDF.
4. When a PDF arrives, the bot:
   - Downloads the file into memory (it is **never written to disk**).
   - **Primary extraction method — Gemini native PDF understanding:** the
     original PDF bytes are sent directly to Google Gemini using its
     built-in document/PDF input capability (no OCR, no regex). Gemini
     reads the document itself and returns a strict JSON object with
     `load_number`, `pickup_date`, `delivery_date`, `pickup_address`,
     `delivery_address`, `weight`, `equipment`, `reference_numbers`,
     `customer`, `broker`, and `notes`. Fields it can't find are returned as
     empty strings rather than guessed.
   - **Fallback extraction method — text + regex:** only if the Gemini PDF
     call fails outright (bad JSON, timeout, repeated API error) does the
     bot fall back to extracting raw text locally with **PyMuPDF** (falling
     back to **pdfplumber** if needed) and parsing it with a set of
     resilient regular expressions. This exists purely as a safety net so
     the bot never simply gives up on a PDF.
   - Reformats the weight into the `NNk lbs` style (e.g. `39847` → `39k lbs`).
   - Geocodes the pickup and delivery addresses and requests a driving route
     from **OpenRouteService**, then rounds the distance to the nearest mile
     and formats it as `Miles :NNN + - dh` (DH is always `-`). The result is
     inserted into the extracted data.
   - Builds a second prompt containing the user's exact template plus the
     extracted (and now mileage-enriched) data, and sends it to **Google
     Gemini** again, instructing it to replace only the relevant values and
     leave everything else — wording, punctuation, line breaks — untouched.
   - Sends the finished Load Confirmation text back to the user.
5. The PDF bytes are discarded immediately after processing; nothing is
   ever stored on disk.

---

## Installing Python

You need **Python 3.12** (or newer).

- **Windows**: download the installer from
  [python.org/downloads](https://www.python.org/downloads/) and run it
  (tick "Add Python to PATH").
- **macOS**: `brew install python@3.12` (requires [Homebrew](https://brew.sh)).
- **Linux (Debian/Ubuntu)**:
  ```bash
  sudo apt update
  sudo apt install python3.12 python3.12-venv python3-pip
  ```

Verify with:
```bash
python3 --version
```

---

## Creating a Telegram Bot & getting a Bot Token

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot`.
3. Choose a display name, then a unique username ending in `bot`
   (e.g. `MyLoadConfirmationBot`).
4. BotFather will reply with a token that looks like:
   `123456789:AAExampleTokenStringGoesHere`
5. Copy this value — it is your `BOT_TOKEN`.
6. (Optional) Send `/setprivacy` to BotFather and disable privacy mode if you
   want the bot to see all messages in group chats — not required for
   normal 1‑on‑1 usage.

---

## Getting a Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey).
2. Sign in with a Google account.
3. Click **Create API key** (choose or create a Google Cloud project if
   prompted).
4. Copy the generated key — this is your `GEMINI_API_KEY`.

---

## Getting an OpenRouteService API Key

1. Go to [openrouteservice.org](https://openrouteservice.org/dev/#/signup)
   and create a free account.
2. After confirming your email, log in to the
   [dashboard](https://openrouteservice.org/dev/#/home).
3. Click **Request a token**, choose the **Standard** (free) plan.
4. Copy the generated key — this is your `ORS_API_KEY`.

The free plan is more than enough for normal bot usage (2,000
geocoding + 2,000 directions requests/day).

---

## Creating a Railway PostgreSQL database

1. Go to [railway.app](https://railway.app) and sign in (GitHub login is
   easiest).
2. Click **New Project** → **Provision PostgreSQL**.
3. Railway will spin up a Postgres instance automatically — no extra
   configuration required.

---

## Getting DATABASE_URL

1. Open your new PostgreSQL service in the Railway dashboard.
2. Click on the **Variables** tab.
3. Copy the value of `DATABASE_URL` (it looks like
   `postgresql://postgres:password@host.railway.app:1234/railway`).
4. Use this value directly as your `DATABASE_URL` environment variable.

---

## Setting ADMIN_ID

`ADMIN_ID` is your personal Telegram numeric user ID — the only account
that can access `/admin` and `/clearcache`.

To find your Telegram ID:
1. Open Telegram and search for **@userinfobot** (or **@RawDataBot**).
2. Start a chat with it — it will reply with your numeric ID.
3. Use that number as `ADMIN_ID`.

---

## Creating your .env file

Create a file named `.env` in the same folder as `main.py`:

```env
BOT_TOKEN=123456789:AAExampleTokenStringGoesHere
GEMINI_API_KEY=your_gemini_api_key
ORS_API_KEY=your_openrouteservice_api_key
DATABASE_URL=postgresql://postgres:password@host.railway.app:1234/railway
ADMIN_ID=123456789
```

Never commit this file to version control — add `.env` to your
`.gitignore`.

---

## Installing dependencies

Create and activate a virtual environment (recommended), then install:

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## Running locally

Once your `.env` is filled in and dependencies are installed:

```bash
python main.py
```

You should see log lines confirming the database pool was created and the
tables were verified, followed by the bot starting polling. Open Telegram
and send `/start` to your bot.

---

## Deploying on Railway

1. Push this project (with `main.py`, `requirements.txt`, and this
   `README.md`) to a GitHub repository. **Do not** commit your `.env` file.
2. In Railway, click **New Project** → **Deploy from GitHub repo** and
   select your repository (you can add it to the same project as your
   PostgreSQL database so they share a private network).
3. Go to the new service's **Variables** tab and add: `BOT_TOKEN`,
   `GEMINI_API_KEY`, `ORS_API_KEY`, `ADMIN_ID`. For `DATABASE_URL`, reference
   the Postgres service's variable (Railway lets you do
   `${{Postgres.DATABASE_URL}}`) so it always stays in sync.
4. Railway auto-detects the Python app and will run
   `pip install -r requirements.txt` then `python main.py` (you can set a
   custom **Start Command** of `python main.py` under Settings if needed).
5. Deploy. Check the **Deployments → Logs** tab to confirm the bot started
   successfully.

The bot is fully async and uses a small connection pool, so it fits
comfortably within the Railway Free Plan's resource limits, and polling
keeps it alive without needing a public URL/webhook.

---

## Deploying on Render

1. Push the project to GitHub as above.
2. In [Render](https://render.com), click **New** → **Background Worker**
   (a worker is correct here since the bot uses long-polling, not a web
   server).
3. Connect your repository.
4. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
5. Add the same environment variables (`BOT_TOKEN`, `GEMINI_API_KEY`,
   `ORS_API_KEY`, `DATABASE_URL`, `ADMIN_ID`) under **Environment**.
6. Deploy. Check the logs to confirm startup.

You can use Render's own managed PostgreSQL, or keep using your Railway
PostgreSQL instance — either works, since `DATABASE_URL` is just a
connection string.

---

## Environment variables reference

| Variable          | Description                                              |
|-------------------|-----------------------------------------------------------|
| `BOT_TOKEN`       | Telegram bot token from BotFather                          |
| `GEMINI_API_KEY`  | Google Gemini API key from AI Studio                        |
| `ORS_API_KEY`     | OpenRouteService API key                                    |
| `DATABASE_URL`    | PostgreSQL connection string                                |
| `ADMIN_ID`        | Your numeric Telegram ID (enables `/admin`, `/clearcache`)  |

---

## How the template system works

- Every user has **exactly one** template, stored in the `templates` table
  (`telegram_id`, `template`, `updated_at`).
- The first time a user interacts with the bot, they're asked to paste
  their Load Confirmation template as plain text.
- Sending `/template` shows the current template and lets the user replace
  it by simply sending new text (an `INSERT ... ON CONFLICT DO UPDATE`
  keeps only the latest version).
- `/delete` removes the saved template so the bot will ask for a new one
  next time.
- The template's exact wording, punctuation and line breaks are preserved —
  Gemini is explicitly instructed to substitute only the data fields and
  never rewrite anything else.

---

## How the cache works

- Templates are cached in a plain in-memory dictionary (`telegram_id ->
  template`) protected by an `asyncio.Lock`.
- Every read first checks the cache; only on a cache miss does the bot
  query PostgreSQL, after which the result is stored back in the cache.
- Every write (save or delete) updates both PostgreSQL and the cache, so
  they never go out of sync during normal operation.
- `/clearcache` (admin only) empties the whole cache, forcing the next
  read for each user to come from the database again.
- The cache is purely in-process RAM: it resets whenever the bot process
  restarts, which is safe since PostgreSQL remains the durable source of
  truth.

---

## How the admin panel works

`/admin` (only usable by `ADMIN_ID`) opens an inline keyboard with:

- **Total Users** / **Templates Count** / **Today's Users** — quick counts.
- **DB Stats** — combined snapshot (users, templates, cached templates).
- **Broadcast** — prompts for a message, then sends it to every known user,
  with a small delay between sends and a final "sent/failed" summary.
- **Search User** — look up a user by numeric Telegram ID or `@username`.
- **View Template** / **Delete Template** — inspect or remove any user's
  saved template by their Telegram ID.
- **Clear Cache** — same as `/clearcache`.
- **Export Users (CSV)** — downloadable CSV of the full `users` table.
- **Export Templates (JSON)** — downloadable JSON dump of the `templates`
  table.
- **Backup Database (SQL)** — a downloadable `.sql` file containing
  `CREATE TABLE` statements plus `INSERT` statements for every row in
  `users` and `templates`, so you can recreate the data on any PostgreSQL
  instance with `psql -f database_backup.sql`.

---

## Available commands

| Command      | Description                                       |
|--------------|----------------------------------------------------|
| `/start`     | Start or restart the bot                            |
| `/help`      | Show help                                           |
| `/template`  | View or replace your saved template                 |
| `/delete`    | Delete your saved template                          |
| `/admin`     | Open the admin panel (admin only)                   |
| `/clearcache`| Clear the in-RAM template cache (admin only)        |

---

## Common errors & troubleshooting

**"Missing required environment variables"**
Your `.env` file is missing one of `BOT_TOKEN`, `GEMINI_API_KEY`,
`ORS_API_KEY`, or `DATABASE_URL`. Double-check spelling and that the file
is named exactly `.env` and sits next to `main.py`.

**"Could not connect to database after N attempts"**
- Check that `DATABASE_URL` was copied correctly from Railway's Variables
  tab (it must include the username, password, host, port and database
  name).
- Confirm the Postgres service is actually running in Railway.
- If deploying elsewhere, make sure outbound access to Railway's Postgres
  host/port is allowed.

**Bot doesn't respond to `/start`**
- Confirm `BOT_TOKEN` is correct and the bot hasn't been re-generated in
  BotFather since.
- Check the logs (`bot.log` or your platform's log viewer) for startup
  errors.
- Make sure only **one** instance of the bot is running — Telegram
  rejects concurrent polling sessions for the same token (you'll see a
  409 Conflict error).

**"I couldn't read that PDF" / pdf_parse_error**
- This only appears if **both** extraction methods fail: Gemini's native PDF
  understanding (primary) and the local PyMuPDF/pdfplumber + regex fallback.
- The file may be a scanned image PDF with no embedded text layer that
  Gemini also can't interpret meaningfully. Try a text-based export of the
  same document.
- The file may be corrupted — re-export or re-download it and try again.

**"I couldn't calculate the mileage for these addresses" / ors_error**
- The pickup or delivery address extracted from the PDF may be incomplete
  or ambiguous (e.g. missing city/state). Check the PDF's formatting.
- Confirm your `ORS_API_KEY` is valid and you haven't exceeded the free
  daily quota (2,000 requests/day).

**"I couldn't generate the Load Confirmation right now" / gemini_error**
- This usually means a transient Gemini API error or timeout. The bot
  already retries once automatically — if it still fails, wait a minute
  and resend the PDF.
- Confirm `GEMINI_API_KEY` is valid and Gemini API access is enabled for
  your Google Cloud project.

**Admin panel says "This command is for administrators only"**
- `ADMIN_ID` in your `.env`/environment doesn't match your numeric
  Telegram ID. Re-check the value with @userinfobot and make sure there
  are no extra spaces.

**Bot stops responding after a while on a free hosting tier**
- Make sure you're deploying as a **worker/background service**, not a
  web service that free tiers may spin down when idle — the bot uses
  long-polling and needs to run continuously.