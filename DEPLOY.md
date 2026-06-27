# Deployment Guide

Get the API live on a fresh Linux VPS in under 30 minutes.

## What You Need

- A Linux VPS — Ubuntu 22.04 recommended
  - Minimum: 1 vCPU, 1 GB RAM, 20 GB disk (~$6/month on Hetzner or DigitalOcean)
- A domain name pointed at that server's IP (e.g. `api.yourcompany.com`)
- SSH access to the server

---

## Step 1 — Provision the Server

SSH into your VPS as root, then run:

```bash
# Update system
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install Docker Compose plugin
apt install -y docker-compose-plugin

# Create a non-root deploy user
adduser deploy
usermod -aG docker deploy
```

---

## Step 2 — Get the Code on the Server

On your own laptop:

```bash
git clone https://github.com/hnasir112/test_pets.git
cd test_pets
git checkout claude/roma-pets-app-analysis-1yq7zj
```

Copy it to the server (run this on your laptop):

```bash
scp -r . deploy@YOUR_SERVER_IP:/home/deploy/gcc-underwriting
```

Or if you make the repo public, just clone it directly on the server:

```bash
# On the server
su - deploy
git clone https://github.com/hnasir112/test_pets.git gcc-underwriting
cd gcc-underwriting
git checkout claude/roma-pets-app-analysis-1yq7zj
```

---

## Step 3 — Set Up Environment Variables

On the server, inside the project folder:

```bash
cp .env.example .env
nano .env
```

Fill in real values:

```
POSTGRES_USER=underwriter
POSTGRES_PASSWORD=<generate a strong password>
POSTGRES_DB=gcc_underwriting
API_SECRET_KEY=<generate a strong secret — this is your admin key>
ENVIRONMENT=production
```

Generate strong values:
```bash
# For POSTGRES_PASSWORD
python3 -c "import secrets; print(secrets.token_hex(24))"

# For API_SECRET_KEY (this becomes your X-Admin-Key)
python3 -c "import secrets; print('admin_' + secrets.token_hex(32))"
```

---

## Step 4 — Set Up SSL Certificate

```bash
# Install certbot
apt install -y certbot

# Get certificate (replace with your actual domain)
certbot certonly --standalone -d api.yourcompany.com

# Copy certs where Nginx expects them
mkdir -p nginx/certs
cp /etc/letsencrypt/live/api.yourcompany.com/fullchain.pem nginx/certs/
cp /etc/letsencrypt/live/api.yourcompany.com/privkey.pem nginx/certs/
chmod 600 nginx/certs/privkey.pem
```

Update `nginx/nginx.conf` — replace `api.yourdomainhere.com` with your actual domain.

---

## Step 5 — Start Everything

```bash
# Build and start all services
docker compose up -d --build

# Watch logs to confirm startup (Ctrl+C to stop watching)
docker compose logs -f
```

You should see:
```
api   | INFO:     Started server process
api   | INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## Step 6 — Create the Database Tables

```bash
docker compose exec api python scripts/init_db.py
```

Expected output:
```
Creating database tables...
Done.
Tables present: api_keys, assessment_reports, anomaly_flags, assessments, institutions, sme_profiles, transactions
```

---

## Step 7 — Verify It Works

```bash
# Health check
curl https://api.yourcompany.com/health

# Expected:
# {"status":"ok","version":"0.1.0"}
```

Open the Swagger docs in a browser:
```
https://api.yourcompany.com/docs
```

---

## Step 8 — Onboard Your First Bank Client

```bash
# 1. Create an institution (replace YOUR_ADMIN_KEY with your API_SECRET_KEY)
curl -X POST https://api.yourcompany.com/v1/admin/institutions \
  -H "X-Admin-Key: YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "National Bank of Bahrain", "tier": "pilot"}'

# → copy the "id" from the response

# 2. Issue them an API key
curl -X POST https://api.yourcompany.com/v1/admin/institutions/INSTITUTION_ID/api-keys \
  -H "X-Admin-Key: YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"label": "nbb-production-key-1"}'

# → copy "raw_key" from response — send this to the bank, shown once only
```

---

## Keeping It Running

**Auto-renew SSL certs** (add to crontab):
```bash
crontab -e
# Add this line:
0 3 * * * certbot renew --quiet && cp /etc/letsencrypt/live/api.yourcompany.com/*.pem /home/deploy/gcc-underwriting/nginx/certs/ && docker compose -f /home/deploy/gcc-underwriting/docker-compose.yml restart nginx
```

**Restart after server reboot** — Docker Compose services have `restart: unless-stopped` so they come back up automatically.

**Deploy a code update:**
```bash
cd /home/deploy/gcc-underwriting
git pull
docker compose up -d --build api
```

**Check logs:**
```bash
docker compose logs api --tail=100 -f
```

**Database backup:**
```bash
docker compose exec db pg_dump -U underwriter gcc_underwriting > backup_$(date +%Y%m%d).sql
```

---

## Costs

| Item | Cost |
|---|---|
| Hetzner CX22 VPS (2 vCPU, 4 GB) | ~€4/month |
| Domain name | ~$12/year |
| SSL certificate (Let's Encrypt) | Free |
| **Total** | **~$6/month** |
