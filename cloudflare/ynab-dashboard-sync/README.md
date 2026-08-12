# Cloudflare sync scheduler

This Worker runs on the `*/5 * * * *` Cron Trigger and dispatches the
repository's `ynab-sync.yml` GitHub Action. The action pulls YNAB with the
repository secret, rebuilds the dashboard, and publishes it to Cloudflare
Pages. The YNAB token is never sent to the browser or stored in this Worker.

## One-time setup

From this directory, deploy the Worker with Wrangler and add the GitHub token
as a Worker secret:

```powershell
npx wrangler@4 deploy
npx wrangler@4 secret put GITHUB_TOKEN
```

The GitHub token needs permission to dispatch workflows in this repository
(for a fine-grained token, grant repository **Contents: read and write**).
The repository also needs these Actions secrets:

- `YNAB_TOKEN`
- `CLOUDFLARE_API_TOKEN` (Pages Edit permission)
- `CLOUDFLARE_ACCOUNT_ID`

After the workflow file is merged into the repository's default branch, the
Worker will trigger it every five minutes. The first run can be started from
the **Actions → Sync YNAB dashboard → Run workflow** button.
