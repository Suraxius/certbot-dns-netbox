#!/usr/bin/env python3
import os
import sys
import requests
import urllib3
import json
from typing import Union

base_url = os.environ.get("NETBOX_URL", None)
bearer = os.environ.get("NETBOX_BEARER", None)
token = os.environ.get("NETBOX_TOKEN", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

DEBUG = bool(os.environ.get("DEBUG", False))

if not base_url:
    print("❌ Missing required environment variable NETBOX_URL")
    sys.exit(1)
if not bearer and not token:
    print("❌ Missing required environment variables NETBOX_BEARER or NETBOX_TOKEN")
    sys.exit(1)


# Clean URL
base_url = base_url.rstrip("/")

ssl_verify = True
timeout = 60

# Following for legacy code. Replace with direct header var access:
headers = {
    "Authorization": f"Bearer {bearer}" if bearer else f"Token {token}",
    "Content-Type": "application/json",
}

session = requests.Session()
session.headers.update(headers)


def _log_debug(msg: str):
    if DEBUG:
        print(msg)


def _raise_with_body(e, r, method="", path="", query=None, data=None):
    """Re-raise *e* enriched with the NetBox API response body and request
    context when it is an HTTPError.

    NetBox returns detailed validation errors in the response body (e.g.
    {"mac_address": ["Enter a valid MAC address."]}), but requests'
    raise_for_status() produces an HTTPError whose str() carries only the
    status code and URL. This rebuilds the exception so str() includes
    the API message and the request context (method, path, filters, data),
    making failures debuggable without DEBUG mode.
    Non-HTTPError exceptions (e.g. JSONDecodeError) are re-raised unchanged.
    """
    if not isinstance(e, requests.exceptions.HTTPError):
        raise e

    try:
        body = r.json()
    except (ValueError, json.JSONDecodeError):
        body = (r.text or "")[:1000]

    if isinstance(body, dict):
        parts = []
        for k, v in body.items():
            if isinstance(v, list):
                parts.append(f"{k}: {', '.join(str(x) for x in v)}")
            else:
                parts.append(f"{k}: {v}")
        body_str = "; ".join(parts) if parts else str(body)
    else:
        body_str = str(body)

    req_context = ""
    if method or path:
        req_context = f" | Request: {method} {path}"
    if query:
        req_context += f" filters={query}"
    if data:
        req_context += f" data={data}"

    raise requests.exceptions.HTTPError(
        f"{e} | API: {body_str}{req_context}", response=r
    )


def disable_ssl_verify():
    global ssl_verify
    ssl_verify = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ==============================================================================
# ========== Get functions =====================================================
# ==============================================================================
def _get(
    path: str, filters: Union[dict, list] = None, raw_text: bool = False
) -> Union[dict, str]:
    # Disable object limit to ensure all matching objects are returned.
    if isinstance(filters, dict):
        if "limit" not in filters:
            filters["limit"] = 0
    elif isinstance(filters, list):
        index = next((v for k, v in filters if k == "limit"), None)
        if index is None:
            filters.append(("limit", 0))
    else:
        raise Exception(
            f"_get() called with unsupported filters type '{type(filters)}'"
        )

    path = path.strip("/")
    url = f"{base_url}/api/{path}/"
    try:
        r = session.get(url, params=filters or {}, verify=ssl_verify, timeout=timeout)
        r.raise_for_status()
        return r.text if raw_text else r.json()
    except (requests.exceptions.HTTPError, json.JSONDecodeError) as e:
        _raise_with_body(e, r, method="GET", path=path, query=filters)


def get_resource(
    path: str, filters: Union[dict, list], multi_exp: bool = False
) -> Union[dict, None]:
    data = _get(path, filters)
    count = data["count"]
    # No Object found
    if count == 0:
        _log_debug(f"No object found using filter {filters}")
        return None

    # If multiple objects expected, return them all:
    elif multi_exp:
        return data["results"]

    # If single object is expected, return it without list:
    elif count == 1:
        return data["results"][0]

    # Single object expected, but multiple found, warn user and return nothing.
    else:
        _log_debug(
            f"Error: Multiple objects found using filter {filters}. Refine your query!"
        )
        return None


# ==============================================================================
# ========== Update functions ==================================================
# ==============================================================================
def _patch(path: str, obj_id: int, data: dict) -> bool:
    path = path.strip("/")
    url = f"{base_url}/api/{path}/{obj_id}/"
    try:
        r = session.patch(url, json=data, verify=ssl_verify, timeout=timeout)
        r.raise_for_status()
        if r.status_code in (200, 204):
            return True
        else:
            _log_debug("Update failed:")
            _log_debug(r.text)
            return False

    except requests.exceptions.HTTPError as e:
        _raise_with_body(e, r, method="PATCH", path=f"{path}/{obj_id}", data=data)


def _patch_resource(path: str, data: dict) -> Union[dict, None]:
    obj_id = data.get("id", None)
    # Dont execute if no ID is set
    if not obj_id:
        _log_debug("Error: No ID supplied when patching resource!")
        return False
    else:
        return _patch(
            path,
            obj_id,
            data,
        )


def modify_resource(path: str, data: dict) -> Union[dict, None]:
    return _patch_resource(path, data)


# ==============================================================================
# ========== Create functions ==================================================
# ==============================================================================
def _post(path: str, data: dict) -> Union[dict, list]:
    path = path.strip("/")
    url = f"{base_url}/api/{path}/"
    try:
        r = session.post(url, json=data, verify=ssl_verify, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except (requests.exceptions.HTTPError, json.JSONDecodeError) as e:
        _raise_with_body(e, r, method="POST", path=path, data=data)


def _post_resource(path: str, data: dict) -> Union[dict, None]:
    if data.get("id"):
        _log_debug("Error: ID supplied when creating resource!")
        return False
    else:
        return _post(
            path,
            data,
        )


def create_resource(path: str, data: dict) -> Union[dict, None]:
    return _post_resource(path, data)


# ==============================================================================
# ========== Delete functions ==================================================
# ==============================================================================
def _delete(path: str, obj_id: int) -> bool:
    path = path.strip("/")
    url = f"{base_url}/api/{path}/{obj_id}/"
    try:
        r = session.delete(url, verify=ssl_verify, timeout=timeout)
        r.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        _log_debug(f"HTTPError: {e}\nResponse body: {r.text}")
        return False


def delete_resource(path: str, obj_id: int) -> bool:
    return _delete(path, obj_id)


# ==============================================================================
# ========== Export functions ==================================================
# ==============================================================================
def export(path: str, filters: Union[dict, list], template: str) -> str:
    if isinstance(filters, dict):
        filters["export"] = template
    elif isinstance(filters, list):
        filters.append(("export", template))
    else:
        raise Exception(
            f"export() called with unsupported filters type '{type(filters)}'"
        )
    return _get(path, filters, raw_text=True)


# ==============================================================================
# ========== Resource-specific wrappers (table-driven) =========================
# ==============================================================================
# Single source of truth mapping resource name -> NetBox API path. The loop
# below generates get_/modify_/create_/delete_ convenience wrappers for every
# entry, delegating to the generic core functions above. Adding a new resource
# is a one-line change to RESOURCES.
RESOURCES = {
    "device": "/dcim/devices",
    "device_interface": "/dcim/interfaces",
    "device_role": "/dcim/device-roles",
    "inventory_item": "/dcim/inventory-items",
    "site": "/dcim/sites",
    "location": "/dcim/locations",
    "rack": "/dcim/racks",
    "cable": "/dcim/cables",
    "platform": "/dcim/platforms",
    "tenant": "/tenancy/tenants",
    "vm": "/virtualization/virtual-machines",
    "vm_interface": "/virtualization/interfaces",
    "virtual_disk": "/virtualization/virtual-disks",
    "ipaddress": "/ipam/ip-addresses",
    "vlan": "/ipam/vlans",
    "dns_view": "/plugins/netbox-dns/views/",
    "dns_zone": "/plugins/netbox-dns/zones/",
    "dns_record": "/plugins/netbox-dns/records/"
}


def _make_getter(path: str):
    def _wrapper(filters: Union[dict, list], multi_exp: bool = False):
        return get_resource(path, filters, multi_exp)

    return _wrapper


def _make_modifier(path: str):
    def _wrapper(data: dict):
        return _patch_resource(path, data)

    return _wrapper


def _make_creator(path: str):
    def _wrapper(data: dict):
        return _post_resource(path, data)

    return _wrapper


def _make_deleter(path: str):
    def _wrapper(obj_id: int):
        return _delete(path, obj_id)

    return _wrapper


for _name, _path in RESOURCES.items():
    for _verb, _factory in (
        ("get", _make_getter),
        ("modify", _make_modifier),
        ("create", _make_creator),
        ("delete", _make_deleter),
    ):
        _fn = _factory(_path)
        _fn.__name__ = f"{_verb}_{_name}"
        _fn.__qualname__ = f"{_verb}_{_name}"
        globals()[f"{_verb}_{_name}"] = _fn
