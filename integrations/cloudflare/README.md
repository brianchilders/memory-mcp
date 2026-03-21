# Cloudflare Tunnel for memory-mcp

This guide explains how to expose memory-mcp safely to the internet using
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

This is required if you want remote callers — such as OpenHome abilities running
in the cloud, or any device not on your local network — to reach your
memory-mcp server.

> **Official docs are always the ground truth.**
> Cloudflare evolves quickly. When in doubt, consult the links throughout
> this guide rather than relying solely on the examples here.

---

## Why Cloudflare Tunnel

Memory-mcp runs on your local machine or LAN. Remote callers cannot reach it
directly without port-forwarding, a dynamic DNS service, and an open firewall
rule — all of which expose your home IP address and attack surface.

Cloudflare Tunnel solves this without any of that:

- `cloudflared` runs on your machine and makes **outbound-only** connections
  to Cloudflare's network. No inbound firewall rules. No open ports. No
  exposed IP.
- Cloudflare terminates HTTPS for you — your callers get a proper
  `https://memory.yourdomain.com` URL with a valid certificate.
- All traffic passes through Cloudflare's DDoS protection and WAF before
  it ever reaches your machine.
- Optionally, [Cloudflare Access](#layer-2-cloudflare-access-optional) adds
  a second authentication gate in front of the memory-mcp bearer token.

> **Official reference:** [Cloudflare Tunnel overview](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)

---

## Prerequisites

1. **A Cloudflare account** — free tier is sufficient.
   Sign up at [cloudflare.com](https://cloudflare.com).

2. **A domain on Cloudflare** — the domain's nameservers must point to
   Cloudflare so it can manage DNS records.
   If you don't have one, Cloudflare Registrar offers domains from ~$8/year.

3. **memory-mcp running** on the machine that will run `cloudflared`.
   Confirm it's up:
   ```bash
   curl http://localhost:8900/health
   # Expected: {"ok": true, "entities": ...}
   ```

> **Official reference:** [Cloudflare Tunnel prerequisites](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-local-tunnel/)

---

## Step 1 — Install cloudflared

**macOS:**
```bash
brew install cloudflared
```

**Linux (Debian / Ubuntu):**
```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-public-v2.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] \
  https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
```

**Linux (RHEL / Fedora):**
```bash
curl -fsSL https://pkg.cloudflare.com/cloudflared.repo \
  | sudo tee /etc/yum.repos.d/cloudflared.repo
sudo yum update && sudo yum install cloudflared
```

**Windows:**
Download the installer from the
[cloudflared downloads page](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/),
then verify:
```powershell
.\cloudflared.exe --version
```

> **Official reference:** [Install cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)

---

## Step 2 — Authenticate

```bash
cloudflared tunnel login
```

This opens a browser window. Log in to your Cloudflare account and select
the domain you want to use. Cloudflare writes a certificate to
`~/.cloudflared/cert.pem` that authorises tunnel management for that domain.

---

## Step 3 — Create a named tunnel

```bash
cloudflared tunnel create memory-mcp
```

This generates a tunnel UUID and writes a credentials file to
`~/.cloudflared/<UUID>.json`. Note the UUID — you'll need it in the config.

Verify it was created:
```bash
cloudflared tunnel list
```

> Using a **named tunnel** (rather than a quick tunnel) gives you a stable
> URL that does not change on restart, which is important for any integration
> that stores the URL (OpenHome ability config, HA secrets.yaml, etc.).

---

## Step 4 — Write config.yml

Create `~/.cloudflared/config.yml` with the following content.
Replace the placeholders marked `# ← CHANGE`.

```yaml
# ~/.cloudflared/config.yml
# memory-mcp Cloudflare Tunnel configuration

tunnel:            <your-tunnel-uuid>                              # ← CHANGE
credentials-file:  /home/<your-user>/.cloudflared/<uuid>.json     # ← CHANGE

ingress:
  # memory-mcp HTTP API — the only service exposed by this tunnel
  - hostname: memory.yourdomain.com                               # ← CHANGE
    service:  http://localhost:8900
    originRequest:
      connectTimeout: 10s         # fail fast if memory-mcp is down
      noTLSVerify:    false       # memory-mcp is HTTP locally; Cloudflare handles TLS externally

  # Required catch-all rule — returns 404 for any unmatched hostname
  - service: http_status:404
```

**Finding your tunnel UUID:**
```bash
cloudflared tunnel list
# NAME         ID                                   CREATED              CONNECTIONS
# memory-mcp   xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  2026-03-21T...
```

**Finding your credentials file path:**
```bash
ls ~/.cloudflared/*.json
# /home/brian/.cloudflared/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.json
```

> **Official reference:** [Tunnel configuration file](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/configuration-file/)

---

## Step 5 — Route DNS

This creates a CNAME record in Cloudflare DNS pointing
`memory.yourdomain.com` at your tunnel:

```bash
cloudflared tunnel route dns memory-mcp memory.yourdomain.com   # ← CHANGE hostname
```

Verify the record was created:
```bash
# In the Cloudflare dashboard: DNS → Records
# You should see: memory  CNAME  <uuid>.cfargotunnel.com  (proxied)
```

---

## Step 6 — Run the tunnel

```bash
cloudflared tunnel run memory-mcp
```

Confirm it's working:
```bash
curl https://memory.yourdomain.com/health
# Expected: {"ok": true, "entities": ...}
```

The tunnel is running. The terminal session must stay open, or continue to
[Step 7](#step-7--run-as-a-service-linux) to install it as a system service.

---

## Step 7 — Run as a service (Linux)

Running the tunnel as a `systemd` service ensures it starts automatically on
boot and restarts if it crashes.

```bash
# Install the systemd service (pass config path explicitly to avoid $HOME issues with sudo)
sudo cloudflared --config /home/<your-user>/.cloudflared/config.yml service install

# Start it now
sudo systemctl start cloudflared

# Enable auto-start on boot
sudo systemctl enable cloudflared

# Verify
sudo systemctl status cloudflared
```

To apply config changes after editing `config.yml`:
```bash
sudo systemctl restart cloudflared
```

**macOS (launch agent):**
```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

> **Official reference:** [Run as a service — Linux](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/as-a-service/linux/)

---

## Security

With the tunnel running, `https://memory.yourdomain.com` is publicly reachable.
Memory-mcp's bearer token authentication is your first line of defence — any
request without a valid `Authorization: Bearer <token>` header returns 401.

Two additional hardening options are described below.

### Layer 2: Cloudflare Access (optional but recommended)

Cloudflare Access adds a second authentication gate *in front of* the tunnel.
Even if a bearer token were leaked, a caller without the Access credentials
cannot reach the origin at all — Cloudflare blocks the request at the edge.

For machine-to-machine callers (OpenHome abilities, scripts, HA), use a
**service token** rather than a human identity policy.

**Create a service token:**

1. Go to [Cloudflare One dashboard](https://one.cloudflare.com) →
   **Access → Service Tokens → Create Service Token**
2. Give it a name (e.g. `memory-mcp-openhome`) and choose an expiry duration
3. Copy the **Client ID** and **Client Secret** immediately —
   the Client Secret is shown only once
4. Set a calendar reminder to renew before expiry (Cloudflare can email you
   one week before — enable it in the token settings)

**Create an Access Application:**

1. **Access → Applications → Add an application → Self-hosted**
2. Set the application domain to `memory.yourdomain.com`
3. Add a policy:
   - **Action:** Service Auth
   - **Include rule:** Service Token → select the token you just created
4. Save

**Callers must now send two additional headers on every request:**

```
CF-Access-Client-Id:     <your-client-id>
CF-Access-Client-Secret: <your-client-secret>
```

In addition to the existing memory-mcp bearer token:

```
Authorization: Bearer <memory-mcp-token>
```

See [Integrating with callers](#integrating-with-callers) below for how to
add these headers to each integration.

> **Official reference:** [Cloudflare Access — service tokens](https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/)
> **Official reference:** [Self-hosted applications](https://developers.cloudflare.com/cloudflare-one/applications/configure-apps/self-hosted-apps/)

### Layer 3: IP allowlist (optional)

If your callers have stable IP addresses (a fixed office, a cloud region),
add an IP rule to the Access policy:

1. Edit your Access Application policy
2. Add an **Include** rule: **IP ranges** → enter the allowed CIDRs
3. Change the rule logic to require **both** the service token **and** the IP

This ensures the endpoint is only reachable from known IPs even if both
the bearer token and the service token credentials are compromised.

> **Official reference:** [Access policies](https://developers.cloudflare.com/cloudflare-one/policies/access/)

---

## Integrating with callers

If you have enabled Cloudflare Access with a service token, every caller
must include the two `CF-Access-*` headers alongside the normal bearer token.

### OpenHome abilities

In `integrations/openhome/background.py` and `main.py`, add the Access
headers to `_HEADERS`:

```python
# Add to the _HEADERS dict in background.py and main.py
_HEADERS = {
    "Authorization":          f"Bearer {MEMORY_API_TOKEN}",
    "Content-Type":           "application/json",
    "CF-Access-Client-Id":     "your-client-id-here",      # ← CHANGE
    "CF-Access-Client-Secret": "your-client-secret-here",  # ← CHANGE
}
```

### Home Assistant rest_commands

In `integrations/homeassistant/memory_mcp_package.yaml`, add the headers
to each `rest_command` block:

```yaml
rest_command:
  memory_record:
    url: "https://memory.yourdomain.com/record"
    method: POST
    content_type: "application/json"
    headers:
      Authorization:          !secret memory_mcp_auth_header
      CF-Access-Client-Id:     !secret cf_access_client_id
      CF-Access-Client-Secret: !secret cf_access_client_secret
    payload: ...
```

Add to `secrets.yaml`:
```yaml
cf_access_client_id:     "your-client-id-here"
cf_access_client_secret: "your-client-secret-here"
```

### ha_state_poller.py and background_example.py

Add the Access headers to the `MemoryClient.__init__` headers dict:

```python
headers = {
    "Content-Type":            "application/json",
    "Authorization":           f"Bearer {token}",
    "CF-Access-Client-Id":     os.environ.get("CF_ACCESS_CLIENT_ID", ""),
    "CF-Access-Client-Secret": os.environ.get("CF_ACCESS_CLIENT_SECRET", ""),
}
```

Set the environment variables before running:
```bash
export CF_ACCESS_CLIENT_ID=your-client-id
export CF_ACCESS_CLIENT_SECRET=your-client-secret
```

---

## Renewing service tokens

Service tokens expire. When they do, all callers return **403 Forbidden** from
Cloudflare (not 401 from memory-mcp — the request never reaches your server).

To renew before expiry:

1. Go to [Cloudflare One](https://one.cloudflare.com) →
   **Access → Service Tokens**
2. Click **Refresh** on the expiring token to extend by one year, or **Edit**
   to set a longer duration
3. No credential change is needed — the Client ID and Client Secret stay the same

Enable expiry alerts:
In the token settings, enable **"Notify before token expires"** to receive
an email one week before expiry.

> **Official reference:** [Service token renewal](https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/)

---

## Troubleshooting

**`curl https://memory.yourdomain.com/health` times out**

- Confirm `cloudflared` is running: `systemctl status cloudflared`
- Confirm memory-mcp is running: `curl http://localhost:8900/health`
- Check `cloudflared` logs: `journalctl -u cloudflared -f`
- Verify the DNS CNAME exists in the Cloudflare dashboard

**`curl` returns 1033 (tunnel not running)**

The `cloudflared` process is not connected to Cloudflare's edge. Restart it:
```bash
sudo systemctl restart cloudflared
cloudflared tunnel info memory-mcp   # should show active connections
```

**`curl` returns 403 Forbidden**

Cloudflare Access is rejecting the request. The `CF-Access-Client-Id` /
`CF-Access-Client-Secret` headers are missing or wrong. Verify the headers
are present and match the credentials shown in Access → Service Tokens.
The `/health` endpoint bypasses memory-mcp auth but still goes through Access
if an Access Application is configured — add the CF-Access headers to the test:

```bash
curl -H "CF-Access-Client-Id: your-id" \
     -H "CF-Access-Client-Secret: your-secret" \
     https://memory.yourdomain.com/health
```

**`curl` returns 401 Unauthorized**

The request reached memory-mcp but the bearer token is wrong. Verify
`MEMORY_API_TOKEN` matches the token shown at
`https://memory.yourdomain.com/admin/settings`.

**Tunnel disconnects frequently**

Cloudflare recommends running at least two `cloudflared` instances for
production reliability (each connecting to a different data centre). For
home use, one instance is fine, but the systemd `Restart=on-failure` setting
ensures it recovers automatically. Confirm it is set:
```bash
systemctl cat cloudflared | grep Restart
```

> **Official reference:** [Tunnel availability and failover](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/tunnel-availability/)
