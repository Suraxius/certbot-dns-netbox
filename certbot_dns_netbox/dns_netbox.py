"""NetBox DNS (netbox-plugin-dns) DNS-01 authenticator plugin for Certbot.

The TXT records for the ACME dns-01 challenge are managed exclusively
through the netbox-dns record API endpoint, via the bundled nbapi.py
helper. The zone endpoint is never queried and the plugin never deletes
records: existing challenge TXT records are updated with the challenge
tokens, and a missing record is only created when no free record is left
and the API token is permitted to create it. Records are left in place
afterwards so they can be updated again on the next renewal.

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
search_view = os.environ.get("SEARCH_VIEW") or "_default_"

RECORD_DESCRIPTION = "certbot DNS-01 challenge"


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
            "netbox-dns with the tokens of the dns-01 challenges, creating "
            "additional records only when none are left and the token "
            "permits it."
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
                f"Failed to set TXT record {validation_name}: {e}"
            ) from e

    def _cleanup(self, domain: str, validation_name: str, validation: str) -> None:
        # Records persist — including ones the plugin created — so they can
        # be updated again on the next renewal; deleting them would leave
        # nothing to update.
        logger.debug(
            "Leaving TXT record %s in place (records are reused, not deleted).",
            validation_name,
        )


class _NetboxDnsClient:
    """netbox-dns client built on the bundled nbapi helper.

    Only the record API endpoint is used, so the API token needs no
    permissions on zones. Records are never deleted: existing challenge
    TXT records are updated in place, and a record is only created when
    no free record is left and the token is permitted to create it.
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

        Records claimed earlier in this certbot run are skipped so that
        several tokens for the same name (e.g. apex and wildcard on one
        certificate) stay live simultaneously. Among the remaining records,
        the least recently updated one is chosen, according to the
        timestamps NetBox keeps on the records themselves — that selection
        needs no shared state, so separate certbot invocations for domains
        sharing a record name still claim different records.
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

        free = [
            record
            for record in records
            if not record.get("managed") and record["id"] not in self._claimed
        ]
        if not free:
            self._create_txt_record(record_fqdn, validation, records)
            return

        # least recently updated record first; ties break on the record id.
        target = min(
            free,
            key=lambda record: (
                record.get("last_updated") or "",
                record["id"],
            ),
        )
        patch = {"id": target["id"], "value": validation}
        if self._ttl:
            patch["ttl"] = self._ttl
        self._nbapi.modify_dns_record(patch)
        self._claimed.add(target["id"])
        logger.info(
            "Updated TXT record %s (id %s).", target["fqdn"], target["id"]
        )

    def _resolve_zone(self, fqdn: str, existing_records: list) -> dict:
        """Resolve the zone covering fqdn using the record endpoint only.

        Any existing record exposes the zone it belongs to, so the zone id
        can simply be harvested from it. Without records, candidate zone
        names are probed against the record list filter; a zone without a
        single visible record cannot provide an id and is referenced by
        name instead, which requires view permission on the zone.
        """
        if existing_records:
            zone = existing_records[0]["zone"]
            return {"id": zone["id"], "name": zone["name"]}

        bare = fqdn.rstrip(".").lower()
        for candidate in dns_common.base_domain_name_guesses(bare):
            try:
                results = (
                    self._nbapi.get_dns_record(
                        {"zone": candidate, "view": search_view, "limit": 1},
                        multi_exp=True,
                    )
                    or []
                )
            except requests.exceptions.HTTPError:
                # the record filter rejects zone names that do not exist
                continue
            if results:
                zone = results[0]["zone"]
                return {"id": zone["id"], "name": zone["name"]}
            # the zone exists, but none of its records are visible to this token
            return {"name": candidate}
        raise errors.PluginError(
            f"No zone found for {fqdn!r} in view {search_view!r}."
        )

    def _create_txt_record(
        self, record_fqdn: str, validation: str, existing_records: list
    ) -> None:
        """Create the challenge TXT record via the record endpoint.

        Only called when no free record is left. The zone is resolved
        through the record endpoint (see _resolve_zone); the created
        record joins the pool of pre-provisioned records.
        """
        zone = self._resolve_zone(record_fqdn, existing_records)
        zone_name = zone["name"].rstrip(".").lower()
        bare = record_fqdn.rstrip(".").lower()
        data = {
            "zone": zone.get("id", {"name": zone["name"]}),
            "name": "@" if bare == zone_name else bare[: -(len(zone_name) + 1)],
            "type": "TXT",
            "value": validation,
            "description": RECORD_DESCRIPTION,
        }
        if self._ttl:
            data["ttl"] = self._ttl

        try:
            created = self._nbapi.create_dns_record(data)
        except Exception as create_error:
            message = str(create_error)
            hint = ""
            if "Related object not found" in message:
                hint = (
                    " (referencing the zone by name requires view permission "
                    "on the zone; alternatively pre-provision any record in "
                    "the zone so that its id can be harvested via the record "
                    "endpoint)"
                )
            elif "permission" in message.lower():
                hint = (
                    " (the API token likely lacks the add action for "
                    "netbox-dns records; grant it, or pre-provision the "
                    "record)"
                )
            raise errors.PluginError(
                f"Failed to create TXT record for {record_fqdn}: "
                f"{create_error}{hint}"
            ) from create_error

        record_id = created.get("id") if isinstance(created, dict) else None
        if record_id:
            self._claimed.add(record_id)
        logger.info(
            "Created TXT record %s in zone %s (view %s).",
            data["name"],
            zone_name,
            search_view,
        )
