"""Entry point: python -m infradash --demo | --config config.yaml"""
from __future__ import annotations

import argparse
import re
import sys


def _seconds(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([sm]?)", str(v).strip())
    if not m:
        raise ValueError(f"bad refresh interval {v!r}")
    return float(m.group(1)) * (60 if m.group(2) == "m" else 1)


def main() -> None:
    ap = argparse.ArgumentParser(prog="infradash", description="OPNsense + Proxmox terminal dashboard")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="run against a simulated homelab")
    mode.add_argument("-c", "--config", help="YAML config with opnsense: and/or proxmox: sections")
    ap.add_argument("--refresh", type=float, help="seconds between polls")
    ap.add_argument("--classic", action="store_true", help="use the original (non-btop) layout")
    args = ap.parse_args()

    from .collector import Collector

    if args.demo:
        from .demo import DemoProvider
        provider, refresh = DemoProvider(), args.refresh or 2.0
    else:
        import yaml
        from .live import LiveProvider

        with open(args.config) as fh:
            cfg = yaml.safe_load(fh) or {}
        try:
            provider = LiveProvider(cfg)
            refresh = args.refresh or _seconds(cfg.get("refresh", "5s"))
        except (KeyError, ValueError) as e:
            sys.exit(f"config error: {e}")
    if args.classic:
        from .app import InfraDash
        InfraDash(Collector(provider), refresh).run()
    else:
        from .btop.app import BtopApp
        BtopApp(Collector(provider), refresh).run()


if __name__ == "__main__":
    main()
