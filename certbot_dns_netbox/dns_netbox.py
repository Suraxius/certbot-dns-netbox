"""NetBox DNS (netbox-plugin-dns) DNS-01 authenticator plugin for Certbot.

The TXT records for the ACME dns-01 challenge are managed exclusively
through the netbox-dns record API endpoint, via the bundled nbapi.py
helper. The zone endpoint is never queried and the plugin never creates
or deletes records: the challenge TXT records (created beforehand in
NetBox, one per challenge token) are updated with the challenge tokens
and left in place afterwards.

Connection settings are taken from the environment, exactly as nbapi.py
expects them:

* ``NETBOX_URL``: base URL of the NetBox instance
* ``NETBOX_BEARER`` or ``NETBOX_TOKEN``: NetBox API token
"""

import logging
import os
from typing import Callable, Optional

import requests

from certbot import errors
from certbot.plugins import dns_common

logger = logging.getLogger(__name__)

# netbox-dns view that contains the zone with the challenge TXT record.
search_view = "global"


class Authenticator(dns_common.DNSAuthenticator):
    """DNS-01 authenticator backed by the netbox-dns plugin."""

    description = "Obtain certificates using a DNS TXT record (in netbox-dns)."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = None

    @classmethod
    def add_parser_arguments(
        cls, add: Callable[..., None], default_propagation_seconds: int = 30
    ) -> None:
        super().add_parser_arguments(
            add, default_propagation_seconds=default_propagation_seconds
        )
        add(
            "ttl",
            type=int,
            default=None,
            help="TTL for the challenge TXT record, in seconds "
            "(default: keep the record's current TTL).",
        )
        add(
            "insecure",
            action="store_true",
            help="Do not verify the TLS certificate of the NetBox instance.",
        )

    def more_info(self) -> str:
        return (
            "This plugin updates pre-provisioned DNS TXT records in "
            "netbox-dns with the tokens of the dns-01 challenges."
        )

    def _setup_credentials(self) -> None:
        # nbapi.py reads its connection settings from the environment and
        # terminates the process at import time when they are missing, so
        # verify them here and fail with a proper certbot error instead.
        if not os.environ.get("NETBOX_URL"):
            raise errors.PluginError(
                "Environment variable NETBOX_URL is not set (required by nbapi.py)."
            )
        if not (os.environ.get("NETBOX_BEARER") or os.environ.get("NETBOX_TOKEN")):
            raise errors.PluginError(
                "Environment variable NETBOX_BEARER or NETBOX_TOKEN must be set "
                "(required by nbapi.py)."
            )

        self.client = _NetboxDnsClient(
            ttl=self.conf("ttl"), insecure=self.conf("insecure")
        )

    def _perform(self, domain: str, validation_name: str, validation: str) -> None:
        try:
            self.client.update_txt_record(validation_name, validation)
        except errors.PluginError:
            raise
        except Exception as e:
            raise errors.PluginError(
                f"Failed to update TXT record {validation_name}: {e}"
            ) from e

    def _cleanup(self, domain: str, validation_name: str, validation: str) -> None:
        # The TXT record is managed outside of this plugin and must remain in
        # place so that it can be updated again on the next renewal; deleting
        # it would leave nothing to update.
        logger.debug(
            "Leaving TXT record %s in place (this plugin only updates records).",
            validation_name,
        )


class _NetboxDnsClient:
    """netbox-dns client built on the bundled nbapi helper.

    Only the record API endpoint is used, so the API token needs no
    permissions on zones. Records are never created or deleted: the
    challenge TXT records must be provisioned beforehand in NetBox, one
    per challenge token, and are updated in place.
    """

    def __init__(self, ttl: Optional[int] = None, insecure: bool = False):
        # Deferred import: nbapi.py exits at import time when the NETBOX_*
        # environment variables are missing. Authenticator._setup_credentials()
        # performs the environment checks before this client is constructed.
        from certbot_dns_netbox import nbapi

        self._nbapi = nbapi
        self._ttl = ttl
        # record ids claimed by this certbot run; each challenge token gets
        # its own pre-provisioned record so multiple tokens for the same
        # name (e.g. apex and wildcard) do not overwrite each other.
        self._claimed: set = set()
        if insecure:
            nbapi.disable_ssl_verify()

    @staticmethod
    def _fqdn(record_fqdn: str) -> str:
        # netbox-dns stores record fqdns with a trailing dot.
        return record_fqdn.rstrip(".").lower() + "."

    def _get_txt_records(self, fqdn: str) -> list:
        try:
            return (
                self._nbapi.get_dns_record(
                    {"fqdn": fqdn, "type": "TXT", "view": search_view},
                    multi_exp=True,
                )
                or []
            )
        except requests.exceptions.RequestException as e:
            raise errors.PluginError(
                f"Failed to query netbox-dns records for {fqdn}: {e}"
            ) from e

    def update_txt_record(self, record_fqdn: str, validation: str) -> None:
        """Write the challenge token into one of the pre-provisioned records.

        Each challenge token claims its own record so that several tokens
        for the same name (e.g. apex and wildcard on one certificate) stay
        live simultaneously.
        """
        fqdn = self._fqdn(record_fqdn)
        records = self._get_txt_records(fqdn)

        for record in records:
            if record["value"] == validation and not record.get("managed"):
                self._claimed.add(record["id"])
                logger.debug(
                    "TXT record for %s already has the expected value.",
                    record_fqdn,
                )
                return

        existing = sorted(
            (record for record in records if not record.get("managed")),
            key=lambda record: record["id"],
        )
        free = [record for record in existing if record["id"] not in self._claimed]
        if not free:
            raise errors.PluginError(
                f"Not enough TXT records for {record_fqdn} in view "
                f"{search_view!r}: {len(existing)} record(s) exist, but all of "
                "them are already in use by this certificate request. Add "
                "another TXT record with the same name and rerun certbot."
            )

        target = free[0]
        patch = {"id": target["id"], "value": validation}
        if self._ttl:
            patch["ttl"] = self._ttl
        self._nbapi.modify_dns_record(patch)
        self._claimed.add(target["id"])
        logger.info(
            "Updated TXT record %s (id %s).", target["fqdn"], target["id"]
        )
