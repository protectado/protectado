# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.

# (type, [mots-clés vendor lowercase])  — premier match gagne
_VENDOR_TYPES = [
    ("phone",    ["apple", "iphone", "samsung", "xiaomi", "huawei", "oppo",
                  "fn-link", "fn link", "motorola", "nokia", "murata",
                  "azurewave", "alps alpine", "quanta computer"]),
    ("computer", ["asustek", "asus", "hp inc", "hewlett", "dell", "lenovo",
                  "acer", "msi ", "gigabyte", "intel corp", "raspberry pi",
                  "microsoft"]),
    ("gaming",   ["nintendo", "sony interactive", "valve", "xbox"]),
    ("printer",  ["brother", "canon", "epson", "xerox", "lexmark", "kyocera"]),
    ("router",   ["tp-link", "netgear", "ubiquiti", "cisco", "linksys",
                  "d-link", "zyxel", "arris", "technicolor"]),
    ("tv",       ["lg electronics", "hisense", "tcl", "vizio", "roku",
                  "sony", "philips"]),
    ("iot",      ["ecobee", "nest", "espressif", "tuya", "ikea", "signify",
                  "belkin", "amazon", "shenzhen"]),
]


def _guess_device_type(vendor: str, mac: str) -> str:
    # MAC localement administrée → téléphone en mode confidentialité MAC
    if mac and len(mac) >= 2:
        try:
            if int(mac.split(":")[0], 16) & 0x02:
                return "phone"
        except ValueError:
            pass
    v = vendor.lower()
    for device_type, keywords in _VENDOR_TYPES:
        if any(k in v for k in keywords):
            return device_type
    return "unknown"


class ARPScanner:
    def __init__(self, pihole):
        self.pihole = pihole

    def scan(self) -> list[dict]:
        """Retourne les appareils réseau via Pi-hole FTL (/api/network/devices)."""
        devices = []
        for d in self.pihole.get_network_devices():
            mac    = d.get("mac", "")
            vendor = d.get("vendor", "")
            devices.append({
                "ip":          d["ip"],
                "mac":         mac,
                "vendor":      vendor,
                "hostname":    d.get("hostname", ""),
                "device_type": _guess_device_type(vendor, mac),
            })
        return devices

    def get_active_ips(self) -> set[str]:
        return {d["ip"] for d in self.scan()}
