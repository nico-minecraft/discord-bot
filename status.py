"""Collect battery and resource usage from Linux sysfs + psutil.

Works on laptops and Raspberry Pi. Devices without a battery still
report CPU, RAM, load, and uptime. UPS HATs that expose
/sys/class/power_supply entries are picked up automatically.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

POWER_SUPPLY = Path("/sys/class/power_supply")


@dataclass
class Battery:
    name: str
    status: str | None
    percent: int | None
    power_watts: float | None
    health_percent: int | None


@dataclass
class DeviceStatus:
    hostname: str
    cpu_percent: float
    ram_used_gb: float
    ram_total_gb: float
    ram_percent: float
    load_1: float
    load_5: float
    load_15: float
    uptime_seconds: float
    ac_online: bool | None
    batteries: list[Battery]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    value = _read_text(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _micro_to_unit(value: int | None, divisor: float) -> float | None:
    if value is None or value <= 0:
        return None
    return value / divisor


def _collect_batteries() -> list[Battery]:
    batteries: list[Battery] = []
    if not POWER_SUPPLY.is_dir():
        return batteries

    for device in sorted(POWER_SUPPLY.iterdir()):
        if _read_text(device / "type") != "Battery":
            continue
        if _read_int(device / "present") == 0:
            continue

        energy_full = _read_int(device / "energy_full")
        energy_design = _read_int(device / "energy_full_design")
        charge_full = _read_int(device / "charge_full")
        charge_design = _read_int(device / "charge_full_design")

        health = None
        if energy_full and energy_design:
            health = round(100 * energy_full / energy_design)
        elif charge_full and charge_design:
            health = round(100 * charge_full / charge_design)

        power_now = _read_int(device / "power_now")
        current_now = _read_int(device / "current_now")
        voltage_now = _read_int(device / "voltage_now")
        power_watts = _micro_to_unit(power_now, 1_000_000)
        if power_watts is None and current_now and voltage_now:
            power_watts = (current_now / 1_000_000) * (voltage_now / 1_000_000)

        batteries.append(
            Battery(
                name=_read_text(device / "model_name")
                or _read_text(device / "name")
                or device.name,
                status=_read_text(device / "status"),
                percent=_read_int(device / "capacity"),
                power_watts=power_watts,
                health_percent=health,
            )
        )
    return batteries


def _ac_online() -> bool | None:
    if not POWER_SUPPLY.is_dir():
        return None
    seen = False
    for device in POWER_SUPPLY.iterdir():
        kind = _read_text(device / "type")
        if kind not in {"Mains", "USB"}:
            continue
        online = _read_int(device / "online")
        if online is None:
            continue
        seen = True
        if online == 1:
            return True
    return False if seen else None


def collect_status() -> DeviceStatus:
    vm = psutil.virtual_memory()
    load1, load5, load15 = os.getloadavg()
    return DeviceStatus(
        hostname=socket.gethostname(),
        cpu_percent=psutil.cpu_percent(interval=0.4),
        ram_used_gb=vm.used / (1024**3),
        ram_total_gb=vm.total / (1024**3),
        ram_percent=vm.percent,
        load_1=load1,
        load_5=load5,
        load_15=load15,
        uptime_seconds=time.time() - psutil.boot_time(),
        ac_online=_ac_online(),
        batteries=_collect_batteries(),
    )


def _format_uptime(seconds: float) -> str:
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def format_status(status: DeviceStatus, device_name: str | None = None) -> str:
    title = device_name or status.hostname
    lines = [
        f"**{title}** (`{status.hostname}`)",
        f"**CPU:** {status.cpu_percent:.0f}%",
        f"**RAM:** {status.ram_used_gb:.1f} / {status.ram_total_gb:.1f} GiB ({status.ram_percent:.0f}%)",
        f"**Load:** {status.load_1:.2f} {status.load_5:.2f} {status.load_15:.2f}",
        f"**Uptime:** {_format_uptime(status.uptime_seconds)}",
    ]
    if status.ac_online is True:
        lines.append("**Power:** AC plugged in")
    elif status.ac_online is False:
        lines.append("**Power:** on battery / no AC")

    if not status.batteries:
        lines.append("**Battery:** none detected")
    else:
        for battery in status.batteries:
            bits = [battery.name]
            if battery.percent is not None:
                bits.append(f"{battery.percent}%")
            if battery.status:
                bits.append(battery.status)
            if battery.power_watts:
                bits.append(f"{battery.power_watts:.1f} W")
            if battery.health_percent is not None:
                bits.append(f"health {battery.health_percent}%")
            lines.append("**Battery:** " + " · ".join(bits))
    return "\n".join(lines)


def main() -> None:
    print(format_status(collect_status()))


if __name__ == "__main__":
    main()
