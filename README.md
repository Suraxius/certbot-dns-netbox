# certbot-dns-netbox

Certbot DNS-01 authenticator plugin that updates the challenge TXT record in
[NetBox DNS](https://github.com/sys4/netbox-plugin-dns) (the `netbox-dns`
NetBox plugin) via the bundled `nbapi.py` API helper.

## How it works

For every domain in the certificate request certbot asks this plugin to set an
`_acme-challenge.<domain>` TXT record. The plugin works exclusively through
the netbox-dns **record API endpoint** — the zone endpoint is never queried,
and the plugin **never creates or deletes records**:

1. looks up the existing TXT records for the FQDN (records can be filtered by
   `fqdn`, `zone` and `view` by name),
2. writes the challenge token into the existing record. If the record does
   not exist yet, certbot aborts with an error: create the record once
   beforehand (in the NetBox UI or your tooling) and rerun,
3. leaves the record in place after validation so it can be updated again on
   the next renewal.

Because a single record holds a single token, a certificate request must not
combine a domain and its wildcard (`-d example.com -d '*.example.com'`): both
map to the same `_acme-challenge.example.com` record, and the second token
would overwrite the first. Issue those as separate certificates.

Connection settings are taken from the environment, exactly as `nbapi.py`
expects them:

| Variable        | Purpose                                    |
| --------------- | ------------------------------------------ |
| `NETBOX_URL`    | Base URL of the NetBox instance            |
| `NETBOX_BEARER` | NetBox API token (`Authorization: Bearer`) |
| `NETBOX_TOKEN`  | NetBox API token (`Authorization: Token`)  |
| `DEBUG`         | Optional: verbose nbapi.py logging         |

One of `NETBOX_BEARER` / `NETBOX_TOKEN` is required. The token's user needs
NetBox object permissions to view and change netbox-dns records only — no
zone or view permissions are required.

## Installation

Install into the same Python environment as certbot:

```
pip install ./certbot-dns-netbox
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

Watch the `_acme-challenge` TXT record in the `global` view: its value changes
to the challenge token during the run and remains in place afterwards. The
next renewal updates the same record again.
