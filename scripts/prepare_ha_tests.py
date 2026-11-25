"""Install and verify the real HA component dependencies used by our tests."""

import argparse
import hashlib
import importlib
from importlib import metadata
import inspect
import json
from pathlib import Path
import platform
import subprocess
import sys

import homeassistant
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Resolve the manifest closure using the running Core's official constraints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    args = parser.parse_args()
    constraints = args.constraints
    if not constraints.is_absolute():
        constraints = REPO_ROOT / constraints
    core_root = Path(homeassistant.__file__).resolve().parent
    if constraints.read_bytes() != (core_root / "package_constraints.txt").read_bytes():
        raise SystemExit("Constraints must match the installed Core's official package_constraints.txt")

    pending = ["esphome", "usb", "zeroconf", "assist_satellite"]
    visited: set[str] = set()
    requirements: set[str] = set()
    while pending:
        component = pending.pop()
        if component in visited:
            continue
        visited.add(component)
        manifest = json.loads((core_root / "components" / component / "manifest.json").read_text())
        pending.extend(manifest.get("dependencies", []))
        pending.extend(manifest.get("after_dependencies", []))
        requirements.update(manifest.get("requirements", []))
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--constraint", str(constraints), *sorted(requirements),
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    subprocess.run([sys.executable, "-m", "pip", "check"], cwd=REPO_ROOT, check=True)
    print(f"Platform: {platform.system()} {platform.release()} {platform.machine()}")
    version = metadata.version("homeassistant")
    print(f"Python: {platform.python_version()}; Core: {version}")
    print(f"Constraints SHA-256: {hashlib.sha256(constraints.read_bytes()).hexdigest()}")
    print(f"Components ({len(visited)}): {', '.join(sorted(visited))}")
    for text in sorted(requirements):
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate():
            print(f"{text}: inapplicable marker")
            continue
        resolved = metadata.version(requirement.name)
        if not requirement.specifier.contains(resolved):
            raise SystemExit(f"Unsatisfied manifest requirement: {text}; resolved {resolved}")
        print(f"{text}: {resolved}")
    for name in ("serialx", "pydantic", "pytest-homeassistant-custom-component"):
        try:
            resolved = metadata.version(name)
        except metadata.PackageNotFoundError:
            if name != "pytest-homeassistant-custom-component":
                raise
            resolved = "absent"
        print(f"{name}: {resolved}")
    count = len({canonicalize_name(dist.metadata["Name"]) for dist in metadata.distributions()})
    print(f"Installed distributions (unique canonical names, including pip): {count}")
    for name in (
        "gazetteer_matcher",
        "homeassistant.components.esphome",
        "homeassistant.components.esphome.serial_proxy",
        "homeassistant.components.assist_satellite",
        "homeassistant.components.usb.serial_proxy_stub",
        "homeassistant.helpers.selector",
    ):
        module = importlib.import_module(name)
        print(f"{name}: {Path(module.__file__).resolve().relative_to(core_root.parent)}")
    proxy = importlib.import_module("homeassistant.components.esphome.serial_proxy")
    symbols = ["register_serialx_transport"]
    symbols += ["build_url"] if version == "2026.10.4" else ["build_port_url", "identity_port_info"]
    for name in symbols:
        symbol = getattr(proxy, name)
        if not callable(symbol):
            raise SystemExit(f"Expected callable ESPHome API: {name}")
        print(f"{name}: {Path(inspect.getsourcefile(symbol)).resolve().relative_to(core_root.parent)}")
    from homeassistant.helpers.selector import SerialPortSelector

    if not callable(SerialPortSelector):
        raise SystemExit("SerialPortSelector is unavailable")
    print("Real-module and public-API checks passed.")


if __name__ == "__main__":
    main()
