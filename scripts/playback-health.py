#!/usr/bin/env python3
"""Observe playback admission/HTTP health; never grant admission or change services."""

import argparse
import http.client
import ipaddress
import math
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

LEASE_PATH = Path("/var/lib/homelab-storage/admission/lease")
BOOT_PATH = Path("/proc/sys/kernel/random/boot_id")
UPTIME_PATH = Path("/proc/uptime")
OUTPUT_PATH = Path("/var/lib/prometheus/node-exporter/playback.prom")
UUID = rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
LEASE = re.compile(
    rb"version=1\nstate=(ready|hold)\nboot_id=(" + UUID + rb")\n"
    rb"generation=[0-9a-f]{32}\nexpires_uptime_seconds=(0|[1-9][0-9]{0,9})\n"
)
PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def private_ipv4(value):
    """Only RFC1918 literals; no URL, DNS, credentials, or alternate ports."""
    address = ipaddress.IPv4Address(value)
    if str(address) != value or not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("a literal private IPv4 address is required")
    return str(address)


def bounded_read(path):
    with path.open("rb") as source:
        value = source.read(513)
    if len(value) > 512:
        raise ValueError("input exceeds size limit")
    return value


def trusted(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("untrusted lease input")
    if not directory and info.st_nlink != 1:
        raise ValueError("linked lease input")


def read_lease(path=LEASE_PATH):
    """Pin the trusted parent and reject symlinks, devices, links and writable input."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        trusted(os.fstat(directory), directory=True)
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as source:
            trusted(os.fstat(source.fileno()))
            value = source.read(513)
        if len(value) > 512:
            raise ValueError("lease exceeds size limit")
        return value
    finally:
        os.close(directory)


def admission(lease, boot, uptime):
    """Mirror playback-admission.sh grammar and 0 < remaining <= 45 policy."""
    if any(len(value) > 512 for value in (lease, boot, uptime)):
        return 0, 0
    match = LEASE.fullmatch(lease)
    if (
        not match
        or not re.fullmatch(UUID + rb"\n", boot)
        or not re.fullmatch(rb"[0-9]+\.[0-9]+ [0-9]+\.[0-9]+\n", uptime)
        or match[1] != b"ready"
        or match[2] != boot[:-1]
    ):
        return 0, 0
    remaining = int(match[3]) - float(uptime.split(b" ")[0])
    return (1, remaining) if 0 < remaining <= 45 else (0, 0)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def probe(address, *, open_url=None):
    address = private_ipv4(address)
    open_url = open_url or http_opener().open
    try:
        with open_url(f"http://{address}:8096/jellyfin/health", timeout=3) as response:
            return response.status == 200 and response.read(32) in (
                b"Healthy",
                b"Healthy\n",
                b"Healthy\r\n",
            )
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        return False


def collect(address, *, lease_reader=read_lease, read=bounded_read, probe_fn=probe, now=time.time):
    address = private_ipv4(address)
    # Probe first, then read the lease/clock: an HTTP timeout must not consume
    # the remaining lifetime of the admission observation we publish.
    healthy = probe_fn(address)
    try:
        admitted, remaining = admission(lease_reader(), read(BOOT_PATH), read(UPTIME_PATH))
    except (OSError, ValueError):
        admitted, remaining = 0, 0
    timestamp = now()
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError("invalid observation timestamp")
    return {
        "homeserver_playback_jellyfin_up": int(healthy and admitted),
        "homeserver_playback_storage_admitted": admitted,
        "homeserver_playback_lease_seconds_remaining": remaining,
        "homeserver_playback_observed_timestamp_seconds": timestamp,
    }


def publish(metrics, target=OUTPUT_PATH):
    """Replace the complete textfile only after a successful write and fsync."""
    content = "".join(f"{name} {value}\n" for name, value in metrics.items())
    descriptor, temporary = tempfile.mkstemp(prefix=".playback-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jellyfin-address", required=True, type=private_ipv4)
    args = parser.parse_args()
    try:
        publish(collect(args.jellyfin_address))
    except (OSError, ValueError):
        print("playback health observation could not be published", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
