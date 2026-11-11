# Deployment

## Streamlit Community Cloud

1. Connect your GitHub account in Streamlit Community Cloud and create an app.
2. Select `Abhijay-Nagal/reconciliation-tracker`, branch `main`, and main file `app.py`.
3. Select Python 3.13 in the deployment's advanced settings.
4. Deploy. The root requirements.txt and bundled data directory are sufficient for the default SQLite demo.

[Official deployment documentation](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy).

The initial demo uses fictional data and needs no API keys. Keep uploads fictional as well: this prototype has no authentication, account isolation, or enforced access controls. Self-entered reviewer names are labels, not verified identities. A public app shares its database state across visitors.

## Persistence

SQLite stores state on the app's filesystem. Do not rely on hosted local files surviving restarts, rebuilds or redeployments. For durable investigation/definition/evaluation history, provision a compatible PostgreSQL database, store its connection string as a deployment secret, and expose it as the DATABASE_URL environment variable. This application reads DATABASE_URL from the environment; it does not automatically read a custom secret field as a URL.

Use a URL of the form `postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require`, with your provider's actual TLS settings and URL-encoded credentials. Never commit real connection strings or .streamlit/secrets.toml. Database creation, backups and permissions are hosting responsibilities; the app creates its own application tables.

## Other hosting

Install requirements.txt and start Streamlit from the repository root:

```bash
python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
```

Configure the port to match the hosting provider. No background job scheduler or extra repository is needed. The CLI remains available as `python reconcile.py`.
