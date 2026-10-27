# Certbot DNS Netbox

Certbot DNS-01 authenticator plugin that updates the challenge TXT records in
[NetBox DNS](https://github.com/sys4/netbox-plugin-dns) (the `netbox-dns`
NetBox plugin) via the bundled `nbapi.py` API helper.

***DISCLAIMER: THIS PROJECT IS VIBE CODED USING AN LLM.***

## How it works

For every domain in the certificate request certbot asks this plugin to set an
`_acme-challenge.<domain>` TXT record. The plugin works exclusively through
the netbox-dns **record API endpoint** — the zone endpoint is never queried,
and the plugin **never creates or deletes records**:

1. looks up the existing TXT records for the FQDN (records can be filtered by
   `fqdn`, `zone` and `view` by name),
2. writes each challenge token into one of the pre-provisioned records,
   claiming one record per token,
3. leaves the records in place after validation so they can be updated again
   on the next renewal.

Connection settings are taken from the environment, exactly as `nbapi.py`
expects them:

| Variable        | Purpose                                    |
| --------------- | ------------------------------------------ |
| `NETBOX_URL`    | Base URL of the NetBox instance            |
| `NETBOX_BEARER` | NetBox API token (`Authorization: Bearer`) |
| `NETBOX_TOKEN`  | NetBox API token (`Authorization: Token`)  |
| `SEARCH_VIEW`   | NETBOX DNS View to search for records in.  |
| `DEBUG`         | Optional: verbose nbapi.py logging         |

One of `NETBOX_BEARER` / `NETBOX_TOKEN` is required. The token's user needs
NetBox object permissions to view and change netbox-dns records only — no
zone or view permissions are required.

## Tutorial: end-to-end setup

This walkthrough sets up certificates for `example.com`, including the
wildcard, from scratch. Adapt domains, names and paths to your environment.

### 1. Create the DNS zone

Create the `example.com` zone in netbox-dns, in the view the plugin searches
(`SEARCH_VIEW` environment variable, default: `_default_`), and note the
numeric **zone ID** of the new zone — it is shown in the web UI URL and can
be looked up with:

```
GET /api/plugins/netbox-dns/zones/?name=example.com
```

### 2. Create a NetBox permission

In the NetBox admin interface, create a permission named
`ACME Client - Zone example.com`:

* **Object type**: `DNS › record` only
* **Actions**: view and change — nothing else. The plugin never creates or
  deletes records, and it does not need zone or view permissions.
* **Constraint**:

```json
{"name": "_acme-challenge", "type": "TXT", "zone_id": 0000}
```

`0000` is the zone ID from step 1. The constraint limits the token to TXT
records named `_acme-challenge` in exactly that zone. Note that certificates
for sub-domains (`-d sub.example.com`) use a record named
`_acme-challenge.sub`; in that case, relax the constraint to
`"name__startswith": "_acme-challenge"` instead of the exact `name`.

### 3. Create a service user

* Create a user group, e.g. `ACME Clients`, and assign the permission from
  step 2 to the **group** — any service user added to the group later
  inherits it.
* Create a user, e.g. `certbot`, without a usable login, and make it a member
  of the group.

### 4. Pre-provision the TXT records

The plugin only updates records, so they must exist before the first run.
Create `_acme-challenge` TXT records in the `example.com` zone — one per
challenge token:

| Certificate request                                   | TXT records needed |
| ----------------------------------------------------- | ------------------ |
| `-d example.com` or `-d '*.example.com'`              | 1                  |
| `-d example.com -d '*.example.com'` (apex + wildcard) | 2                  |

All records for a challenge share the same name (`_acme-challenge`, relative
to the zone); netbox-dns allows multiple TXT records with different values
for the same name. Initial values are placeholders and get rewritten by the
plugin. If a certificate request needs more records than exist, certbot
aborts with `Not enough TXT records ...` — add another record and rerun.

### 5. Create an API token and environment file

On the `certbot` user, create an API token (leave write enabled) and expose
it to whatever runs certbot — a cronjob in most cases — via environment
variables. Keep them in a root-readable-only file, e.g.
`/etc/default/certbot-netbox`:

```sh
NETBOX_URL=https://netbox.example.com
NETBOX_TOKEN=<token>     # or: NETBOX_BEARER=<token>
```

```
chmod 600 /etc/default/certbot-netbox
```

### 6. Install certbot and the plugin in a dedicated venv

Do not install certbot from the system package manager — certbot and the
plugin live together in a dedicated venv:

```sh
python3 -m venv /opt/certbot-netbox
/opt/certbot-netbox/bin/pip install certbot certbot-dns-netbox
```

### 7. Test

```sh
/opt/certbot-netbox/bin/certbot certonly \
    --authenticator dns-netbox \
    -d example.com -d '*.example.com'
```

The first ever certbot run asks for an ACME account e-mail and the terms of
service. During the run the plugin writes the challenge tokens into the
`_acme-challenge` TXT records; the records remain in place afterwards. Tip:
append `--dry-run` for a first try to validate the setup against the staging
ACME API without touching rate limits.

### 8. Automate renewal

Create a wrapper that loads the environment and runs certbot from the venv,
e.g. `/usr/local/bin/certbot-netbox-renew`:

```sh
#!/bin/sh
set -a
. /etc/default/certbot-netbox
set +a
exec /opt/certbot-netbox/bin/certbot renew --quiet
```

```
chmod +x /usr/local/bin/certbot-netbox-renew
```

And a cron entry, e.g. `/etc/cron.d/certbot-netbox`:

```
0 3 * * * root /usr/local/bin/certbot-netbox-renew >> /var/log/certbot-netbox-renew.log 2>&1
```

`certbot renew` reads the renewal configuration written by step 7, so no
authenticator flags are needed here. Add a `--deploy-hook` (e.g. restarting
the web server) if services need to pick up the renewed certificate.

## Installation

Install into the same Python environment as certbot:

```
pip install certbot-dns-netbox
```

## Usage

```
export NETBOX_URL=https://netbox.example.com
export NETBOX_BEARER=<token>

certbot certonly \
    --authenticator dns-netbox \
    --dns-netbox-propagation-seconds 60 \
    -d example.com -d '*.example.com'
```

### Options

| Option                             | Default        | Description                          |
| ---------------------------------- | -------------- | ------------------------------------ |
| `--dns-netbox-propagation-seconds` | `30`           | Seconds to wait for DNS propagation  |
| `--dns-netbox-ttl`                 | keep current   | TTL of the challenge TXT record (s)  |
| `--dns-netbox-insecure`            | off            | Skip TLS verification towards NetBox |

The netbox-dns view to search is configured in code: `search_view` at the top
of `certbot_dns_netbox/dns_netbox.py`.

### Renewal configuration

certbot stores the authenticator in the renewal configuration, so renewals
need no extra flags:

```
# /etc/letsencrypt/renewal/example.com.conf
[renewalparams]
authenticator = dns-netbox
dns_netbox_ttl = 120
```

When certbot runs unattended (cron or systemd timer), make sure the `NETBOX_*`
environment variables are set, e.g. via a systemd `EnvironmentFile=` or a
wrapper script.

## Verification

```
certbot plugins --init dns-netbox     # plugin is discovered and loads
certbot certonly --dry-run \
    --authenticator dns-netbox \
    -d sub.example.com                # end-to-end against the staging ACME API
```

Watch the `_acme-challenge` TXT records in the `_default_` view: their values
change to the challenge tokens during the run and remain in place afterwards.
The next renewal updates the same records again.
