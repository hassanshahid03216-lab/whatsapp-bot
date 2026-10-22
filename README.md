# SellerBot

SellerBot is an early seller-facing MVP: seller accounts, a catalog/FAQ dashboard, semantic retrieval, WhatsApp Cloud API webhooks, and a human conversation inbox. It is not a hosted SaaS service yet; each deployment still needs Meta credentials, an HTTPS domain, monitoring, and a support/operations owner.

## Run locally with Docker Desktop

1. Copy `.env.example` to `.env` if you do not already have a local `.env`. Generate a random `SECRET_KEY` and a Fernet `WHATSAPP_ACCESS_TOKEN_KEY` using the commands at the bottom of `.env.example`. Set `DATABASE_URL` to the Compose database host, for example `postgresql://postgres:YOUR_DB_PASSWORD@db:5432/sellerbot`, and make the `POSTGRES_*` values match.
2. Choose an embedding provider and set its key: `EMBEDDING_PROVIDER=gemini` plus `GEMINI_API_KEY`, or `EMBEDDING_PROVIDER=openai` plus `OPENAI_API_KEY`.
3. Start Postgres and migrate, then run the app:

   ```powershell
   docker compose up -d db
   docker compose run --rm api alembic upgrade head
   docker compose up --build api
   ```

4. Open the seller dashboard at `http://localhost:8000/` (or the port in `API_HOST_PORT`). Sign up, upload a CSV using the provided template, and add FAQs. Use `docker compose run --rm api pytest -q` to run the suite.

## Connect a seller's WhatsApp number

1. Create a Meta app and add the WhatsApp product. Use Meta's [Cloud API getting-started guide](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started/) to get the sender's **Phone Number ID** and a long-lived access token with the WhatsApp messaging permission. The token is entered per seller in the dashboard and encrypted at rest using `WHATSAPP_ACCESS_TOKEN_KEY`.
2. Set these deployment values in `.env`: `WHATSAPP_VERIFY_TOKEN` (a private random string), `WHATSAPP_APP_SECRET` (Meta App Settings → Basic), `WHATSAPP_ACCESS_TOKEN_KEY` (Fernet key), and `WHATSAPP_GRAPH_API_VERSION` to a Graph API version Meta currently supports. Keep all of them secret except the version.
3. Deploy behind a public HTTPS domain. Localhost is not reachable by Meta; use a trusted HTTPS tunnel for development or a real domain for a pilot. Configure the Meta webhook callback as `https://YOUR-DOMAIN/webhooks/whatsapp`, enter the exact `WHATSAPP_VERIFY_TOKEN`, verify it, and subscribe the app to the `messages` field. Webhook GET verification and POST `X-Hub-Signature-256` checks are implemented.
4. In the dashboard's WhatsApp setup, save that seller's Phone Number ID and access token. Send a message from an allowed test recipient. The app records inbound/outbound messages, replies to confident active product/FAQ matches, and flags uncertain requests for seller follow-up. Sellers can pause/resume the bot and send manual replies in the inbox.

The automatic replies are deliberately rules-based: price and stock are read from the seller's product row, while FAQ answers come from active FAQs. The app does not use an LLM to invent order, price, or inventory information. Threshold `0.65` is only a starting placeholder; run `python scripts/eval_retrieval.py --seller-id UUID` inside the app container after loading a catalog and tune it against real seller data.

## Before a public commercial launch

- Host the app and database with backups, HTTPS, secrets management, alerting, and a recovery plan. Do not expose Postgres publicly.
- Configure a supported Meta Graph API version and complete Meta's app/business review and phone-number setup for the intended customers. Each seller needs a valid WhatsApp Business Platform setup and token.
- Confirm WhatsApp's current messaging, template, consent, and business policies for the use case. This app replies to inbound messages; it does not implement approved outbound templates, billing, embedded seller onboarding, or payment/order checkout.
- Tune retrieval thresholds with representative positive and negative questions for each catalog, and review human handoff behavior before enabling automation for customers.
- Publish privacy/retention terms and support contact details appropriate to the business and locations where it operates.
