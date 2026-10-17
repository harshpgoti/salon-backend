"""Fail when an installed dependency is unpinned or differs from the tested lock."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import sys


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path):
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.+!-]*)", line)
        if match is None or normalized(match[1]) in pins:
            raise ValueError(f"Invalid exact pin: {line}")
        pins[normalized(match[1])] = match[2]
    if not pins:
        raise ValueError("Invalid exact pin: empty lock")
    return pins


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path,
                        default=Path(__file__).resolve().parents[1] / "constraints.txt")
    parser.add_argument("--inventory", type=Path, help="Compare a recorded distribution inventory JSON")
    args = parser.parse_args()
    try:
        pins = read_pins(args.constraints)
        if args.inventory:
            inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
            if not isinstance(inventory, dict) or not inventory:
                raise ValueError("Invalid inventory: expected nonempty package/version object")
            installed = list(inventory.items())
        else:
            installed = [(d.metadata["Name"], d.version) for d in metadata.distributions()]
        failures = []
        checked = 0
        seen = set()
        for name, version in installed:
            if not isinstance(name, str) or not isinstance(version, str):
                raise ValueError("Invalid inventory: package and version must be strings")
            package = normalized(name)
            if package in {"pip", "salon-api"}:
                continue
            checked += 1
            if package in seen:
                failures.append(f"{package}: duplicate installed distribution")
            seen.add(package)
            if package not in pins:
                failures.append(f"{package}=={version}: unlisted dependency")
            elif version != pins[package]:
                failures.append(f"{package}: installed {version}; locked {pins[package]}")
        if not checked:
            raise ValueError("Invalid inventory: no dependencies")
        if failures:
            print("\n".join(failures))
            return 1
        print(f"Dependency lock matched {checked} installed distributions.")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
