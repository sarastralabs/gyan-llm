# Gyan runbook

How to run Gyan on a server: first-time setup, daily operations, backups, security, and what to do
when something breaks. Written for the Oracle Always Free ARM VM (2 cores, 12 GB), but nothing
here is specific to it. Replace `VM` with your server's address, `KEY` with your SSH key file and
`HOST` with the public hostname.

## What runs where

```
internet ─► nginx (80/443, on the VM itself)
              ├─ /v1/*          ─► gateway   127.0.0.1:8000  (API for other platforms, needs an API key)
              ├─ /api/auth/*    ─► chat UI   127.0.0.1:3080  (tight rate limit)
              └─ everything else ─► chat UI  127.0.0.1:3080
            never public: /admin and /readyz (blocked), Redis, Ollama, SearXNG, MongoDB
```

All services run in Docker (`docker compose`) in `~/gyan` on the VM; data lives in Docker volumes:

| Volume | Holds | Back up? |
|---|---|---|
| `gyan_mongo-data` | UI users and chats | yes |
| `gyan_gateway-data` | API keys and usage (SQLite) | yes |
| `gyan_ollama-models` | the downloaded model | no (re-download) |
| `gyan_librechat-images/uploads/logs` | UI files | not needed |

Typical memory: model 3 to 4 GB, UI about 0.5 GB, MongoDB 0.2 GB, SearXNG 0.1 GB, gateway 0.05 GB.
Each container has a memory limit in `docker-compose.yml` so one cannot starve the others.

## First-time setup

1. **Deploy** (from your computer, in the repo folder):

   ```bash
   bash scripts/deploy.sh ubuntu@VM KEY HOST
   ```

   It copies the repo, creates `.env` with fresh secrets on the server, builds the gateway, pulls
   the model, creates the UI's gateway key and starts everything. The first run downloads about 9 GB.

2. **Create the admin account while the site is still private.** Registration is closed by
   default; open it briefly on the server and register through an SSH tunnel:

   ```bash
   ssh -i KEY ubuntu@VM 'cd ~/gyan && sed -i "s/^ALLOW_REGISTRATION=.*/ALLOW_REGISTRATION=true/" .env && docker compose --profile ui up -d'
   ssh -N -L 3080:127.0.0.1:3080 -i KEY ubuntu@VM        # leave open, then browse http://localhost:3080
   ```

   Register. **The first account becomes the admin.** Then lock it again:

   ```bash
   ssh -i KEY ubuntu@VM 'cd ~/gyan && sed -i "s/^ALLOW_REGISTRATION=.*/ALLOW_REGISTRATION=false/" .env && docker compose --profile ui up -d'
   ```

3. **Open the firewall.** On the server (`sudo bash scripts/setup-firewall.sh`), and in the Oracle
   console: Networking, Virtual Cloud Networks, your VCN, Security Lists, Default, **Add Ingress
   Rules**: source `0.0.0.0/0`, TCP, destination ports `80` and `443` (two rules).

4. **Put nginx in front.** On the server: `sudo bash scripts/setup-nginx.sh HOST`.

5. **HTTPS.** You need a hostname that points at the server. Either a DNS `A` record such as
   `gyan.sarastralabs.com` to the server's IP, or the free `80-225-248-25.sslip.io` style name that
   resolves to the IP in its name. Then, on the server:

   ```bash
   sudo apt install -y certbot python3-certbot-nginx
   sudo certbot --nginx -d HOST --redirect
   ```

   certbot asks for an email and for you to accept Let's Encrypt's terms; you do that, not a
   script. Renewal is automatic (`systemctl list-timers | grep certbot`). Finally tell the UI its
   public address and restart it:

   ```bash
   cd ~/gyan && sed -i "s#^DOMAIN_CLIENT=.*#DOMAIN_CLIENT=https://HOST#" .env && docker compose --profile ui up -d
   ```

6. **Backups.** Add the nightly job (`crontab -e` on the server):

   ```
   17 3 * * * cd $HOME/gyan && bash scripts/backup.sh >> $HOME/backups/backup.log 2>&1
   ```

   Copy backups off the server now and then (a backup on the same machine dies with it):

   ```bash
   scp -i KEY "ubuntu@VM:backups/*" ./gyan-backups/
   ```

## Daily operations

All commands run on the server in `~/gyan` (`ssh -i KEY ubuntu@VM`).

| Task | Command |
|---|---|
| Status | `docker compose --profile ui ps` |
| Logs (follow) | `docker compose --profile ui logs -f gateway` (or `librechat`, `ollama`, `searxng`) |
| Restart one service | `docker compose --profile ui restart librechat` |
| Update to this repo's latest | from your computer: `bash scripts/deploy.sh ubuntu@VM KEY HOST` |
| Who uses the API | `docker compose exec gateway gyan-gateway list-keys` |
| New API key for another platform | `docker compose exec gateway gyan-gateway create-key --name n8n --rpm 30 --daily 1000` (shown once) |
| Disable a key | `docker compose exec gateway gyan-gateway revoke ID` |
| Usage per key (last 24 h) | `source .env; curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8000/admin/usage` |
| Queue and health | `source .env; curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8000/admin/stats` |

The API for other platforms is OpenAI-compatible: base URL `https://HOST/v1`, the key as the API key,
models `qwen2.5:3b` and `gyan-research` (see the main README for examples).

Upgrading the UI: change the `librechat` image tag in `docker-compose.yml`, read the release notes
for config changes, deploy. Upgrading the model: `docker compose exec ollama ollama pull NAME`, set
`DEFAULT_MODEL` and the model names in `ui/librechat.yaml`, deploy.

## Restore from a backup

```bash
# chats and users
docker compose --profile ui exec -T mongodb mongorestore --archive --gzip --drop < ~/backups/mongo-STAMP.archive.gz
# API keys and usage
docker compose cp ~/backups/gateway-STAMP.db gateway:/data/gateway.db && docker compose restart gateway
# secrets (only if .env was lost): cp ~/backups/env-STAMP .env && chmod 600 .env
```

Losing `CREDS_KEY` or `JWT_SECRET` logs everyone out and invalidates stored credentials, so keep the
`env-*` backups safe (they contain secrets).

## Security checklist

- [ ] Registration is off (`ALLOW_REGISTRATION=false`) once the admin exists.
- [ ] Only ports 22, 80 and 443 are open; in the Oracle security list, limit port 22 to your own IP if you can.
- [ ] HTTPS works and `http://` redirects to it.
- [ ] `/admin` is not reachable from outside: `curl -i https://HOST/admin/stats` must return 404.
- [ ] `.env` has mode 600 and is never copied into git or chat.
- [ ] Each platform has its own API key with sensible limits; revoke keys you no longer use.
- [ ] The model server (Ollama), Redis, SearXNG and MongoDB are not published (`ss -ltn` shows them only on 127.0.0.1 or not at all).
- [ ] Backups exist and one copy is off the server.

Known limits: all UI users share one gateway key, so its per-minute limit is shared; MongoDB runs without a
password but is reachable only inside the Docker network; research answers come from a small model and can
be wrong, so the citations must be checked.

## Free-tier cautions (Oracle Always Free)

- **Idle VMs can be reclaimed.** Oracle may stop an Always Free VM whose CPU, network and (ARM) memory use all
  stay under 20% for 7 days. Real use avoids this; an unused server can be stopped without warning.
- **Storage:** the free allowance is 200 GB across all VMs. Docker images take about 9 GB; clean old ones with
  `docker image prune -f` after updates.
- **Do not click Upgrade** to Pay As You Go unless you intend to pay.
- **Costs:** everything here runs on the free VM; Let's Encrypt and sslip.io are free.

## When something breaks

| Symptom | Likely cause and fix |
|---|---|
| UI says it cannot reach the model | The UI's gateway key is missing or revoked: check `GYAN_API_KEY` in `.env`, `gyan-gateway list-keys`; or the gateway is down: `docker compose ps` |
| `503 server_busy` | Many people at once; the model serves one answer at a time. Wait a few seconds, or raise `MAX_QUEUE` |
| Research answers are slow (30 to 60 s) | Normal on 2 cores; lower `RESEARCH_CHARS_TOTAL` to trade detail for speed |
| "No readable web sources" | SearXNG engines were rate-limited; retry later, check `docker compose logs searxng` |
| Out of memory, container restarting | `docker stats --no-stream`; stop other loads; lower `NUM_CTX` in `.env` |
| Disk almost full | `docker system df`; `docker image prune -f`; remove old files in `~/backups` |
| Cannot sign in after a restart | `JWT_SECRET` or `CREDS_KEY` changed: restore `.env` from a backup |
| Site unreachable from outside | Check both firewalls (security list rule and `sudo iptables -S INPUT`), nginx (`sudo nginx -t`, `systemctl status nginx`) |
