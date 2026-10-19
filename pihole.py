"""Pi-hole (v6 API) and connectivity checks for the status report.

Everything here is stdlib-only. The Pi-hole section is enabled by setting
PIHOLE_URL in .env; leave it empty and the section is skipped entirely.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("device-status")

HTTP_TIMEOUT = 4.0
NET_TIMEOUT = 1.5
DNS_TIMEOUT = 2.0
# Public endpoints used only to test raw internet reachability (TCP connect).
INTERNET_TARGETS = (("1.1.1.1", 443), ("9.9.9.9", 443), ("8.8.8.8", 443))


class PiholeError(Exception):
    """A problem worth showing to the user in the status message."""


@dataclass
class PiholeStatus:
    internet_online: bool
    internet_ms: float | None
    dns_ok: bool
    dns_ms: float | None
    api_error: str | None = None
    blocking: str | None = None
    blocking_timer: float | None = None
    hour_total: int | None = None
    hour_blocked: int | None = None
    day_total: int | None = None
    day_blocked: int | None = None
    day_percent_blocked: float | None = None
    domains_on_blocklist: int | None = None
    gravity_updated: float | None = None
    clients_active: int | None = None


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def _base_url() -> str:
    raw = os.getenv("PIHOLE_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    for suffix in ("/api", "/admin"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    return raw


def _verify_tls() -> bool:
    value = os.getenv("PIHOLE_VERIFY_TLS", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def pihole_configured() -> bool:
    return bool(_base_url())


# --------------------------------------------------------------------------
# internet + DNS checks (work even when the Pi-hole API is down)
# --------------------------------------------------------------------------


def _check_internet() -> tuple[bool, float | None]:
    for host, port in INTERNET_TARGETS:
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=NET_TIMEOUT):
                return True, (time.monotonic() - start) * 1000
        except OSError:
            continue
    return False, None


def _dns_packet(name: str) -> tuple[bytes, bytes]:
    qid = os.urandom(2)
    header = qid + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0)  # RD set, 1 question
    qname = b"".join(
        bytes([len(label)]) + label.encode("idna") for label in name.strip(".").split(".")
    )
    return qid, header + qname + b"\x00" + struct.pack("!HH", 1, 1)  # type A, class IN


def _check_dns(server_host: str, domain: str) -> tuple[bool, float | None]:
    """Resolve `domain` through the Pi-hole's DNS port and expect an answer."""
    qid, packet = _dns_packet(domain)
    try:
        targets = socket.getaddrinfo(server_host, 53, socket.AF_UNSPEC, socket.SOCK_DGRAM)
    except OSError:
        return False, None
    for family, _, _, _, sockaddr in targets:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(DNS_TIMEOUT)
                start = time.monotonic()
                sock.sendto(packet, sockaddr)
                data, _ = sock.recvfrom(1232)
                elapsed = (time.monotonic() - start) * 1000
        except OSError:
            continue
        if len(data) < 12 or data[:2] != qid:
            continue
        flags, _qd, answers = struct.unpack("!HHH", data[2:8])
        if (flags & 0xF) == 0 and answers > 0:  # NOERROR with at least one answer
            return True, elapsed
    return False, None


# --------------------------------------------------------------------------
# Pi-hole v6 REST API
# --------------------------------------------------------------------------


def _request(
    base: str,
    path: str,
    *,
    method: str = "GET",
    sid: str | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{base}/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if sid:
        req.add_header("X-FTL-SID", sid)
    context = None
    if url.startswith("https") and not _verify_tls():
        context = ssl._create_unverified_context()  # noqa: SLF001 - self-signed certs
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=context) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PiholeError("API login rejected (check PIHOLE_PASSWORD)") from exc
        if exc.code == 429:
            raise PiholeError("API rate limited, try again shortly") from exc
        raise
    return json.loads(raw) if raw else {}


def _login(base: str, password: str) -> str:
    data = _request(base, "/auth", method="POST", body={"password": password})
    session = data.get("session") or {}
    sid = session.get("sid")
    if not session.get("valid") or not sid:
        raise PiholeError("API login rejected (check PIHOLE_PASSWORD)")
    return sid


def _logout(base: str, sid: str) -> None:
    try:
        _request(base, "/auth", method="DELETE", sid=sid)
    except Exception:  # noqa: BLE001 - sessions expire on their own anyway
        pass


def _safe(base: str, path: str, sid: str | None) -> dict[str, Any] | None:
    try:
        return _request(base, path, sid=sid)
    except PiholeError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Pi-hole %s failed: %s", path, exc)
        return None


def _last_hour(history: list[dict[str, Any]], now: float) -> tuple[int, int]:
    """Sum 10-minute history buckets that started within the last hour."""
    cutoff = now - 3600
    total = blocked = 0
    for slot in history:
        stamp = slot.get("timestamp")
        if stamp is None or stamp <= cutoff:
            continue
        total += int(slot.get("total") or 0)
        blocked += int(slot.get("blocked") or 0)
    return total, blocked


def _fill_from_api(status: PiholeStatus, base: str) -> None:
    password = os.getenv("PIHOLE_PASSWORD", "")
    sid: str | None = None
    try:
        if password:
            sid = _login(base, password)
        summary = _safe(base, "/stats/summary", sid)
        blocking = _safe(base, "/dns/blocking", sid)
        history = _safe(base, "/history", sid)
    except PiholeError as exc:
        status.api_error = str(exc)
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("Pi-hole API unreachable: %s", exc)
        status.api_error = "API unreachable"
        return
    finally:
        if sid:
            _logout(base, sid)

    if summary is None and blocking is None and history is None:
        status.api_error = "API unreachable"
        return

    if summary:
        queries = summary.get("queries") or {}
        clients = summary.get("clients") or {}
        gravity = summary.get("gravity") or {}
        status.day_total = queries.get("total")
        status.day_blocked = queries.get("blocked")
        status.day_percent_blocked = queries.get("percent_blocked")
        status.clients_active = clients.get("active")
        status.domains_on_blocklist = gravity.get("domains_being_blocked")
        status.gravity_updated = gravity.get("last_update") or None
    if blocking:
        status.blocking = blocking.get("blocking")
        status.blocking_timer = blocking.get("timer")
    if history and isinstance(history.get("history"), list):
        status.hour_total, status.hour_blocked = _last_hour(history["history"], time.time())


def collect_pihole() -> PiholeStatus | None:
    base = _base_url()
    if not base:
        return None
    host = urllib.parse.urlparse(base).hostname or "localhost"
    domain = os.getenv("PIHOLE_DNS_TEST_DOMAIN", "").strip() or "cloudflare.com"

    with ThreadPoolExecutor(max_workers=3) as pool:
        net_future = pool.submit(_check_internet)
        dns_future = pool.submit(_check_dns, host, domain)
        api_future = pool.submit(lambda: _api_status(base))
        online, net_ms = net_future.result()
        dns_ok, dns_ms = dns_future.result()
        status = api_future.result()

    status.internet_online = online
    status.internet_ms = net_ms
    status.dns_ok = dns_ok
    status.dns_ms = dns_ms
    return status


def _api_status(base: str) -> PiholeStatus:
    status = PiholeStatus(internet_online=False, internet_ms=None, dns_ok=False, dns_ms=None)
    _fill_from_api(status, base)
    return status


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def _n(value: int | float | None) -> str:
    return "?" if value is None else f"{int(value):,}"


def _pct(part: int | None, whole: int | None) -> str:
    if part is None or not whole:
        return "0%"
    return f"{100 * part / whole:.0f}%"


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{max(1, seconds // 60)}m"


def format_pihole(status: PiholeStatus) -> list[str]:
    lines = ["", "**Pi-hole**"]

    net = "**Internet:** "
    if status.internet_online:
        net += "online" + (f" ({status.internet_ms:.0f} ms)" if status.internet_ms else "")
    else:
        net += "**OFFLINE**"
    net += " · DNS via Pi-hole "
    if status.dns_ok:
        net += "OK" + (f" ({status.dns_ms:.0f} ms)" if status.dns_ms else "")
    else:
        net += "**FAILED**"
    lines.append(net)

    if status.api_error:
        lines.append(f"**API:** {status.api_error}")
        return lines

    if status.blocking:
        text = status.blocking
        if status.blocking != "enabled" and status.blocking_timer:
            text += f" (back on in {_age(status.blocking_timer)})"
        lines.append(f"**Blocking:** {text}")

    if status.hour_total is not None:
        lines.append(
            f"**Last hour:** {_n(status.hour_total)} queries · "
            f"{_n(status.hour_blocked)} blocked ({_pct(status.hour_blocked, status.hour_total)})"
        )

    if status.day_total is not None:
        pct = status.day_percent_blocked
        pct_text = f"{pct:.1f}%" if pct is not None else _pct(status.day_blocked, status.day_total)
        lines.append(
            f"**Last 24h:** {_n(status.day_total)} queries · "
            f"{_n(status.day_blocked)} blocked ({pct_text})"
        )

    if status.domains_on_blocklist is not None:
        text = f"{_n(status.domains_on_blocklist)} domains"
        if status.gravity_updated:
            text += f" · updated {_age(time.time() - status.gravity_updated)} ago"
        lines.append(f"**Blocklist:** {text}")

    if status.clients_active is not None:
        lines.append(f"**Clients:** {_n(status.clients_active)} active")
    return lines
