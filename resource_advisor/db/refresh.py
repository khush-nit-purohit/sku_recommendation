import json
import shutil
import sqlite3
import subprocess
import sys

from .queries import upsert_aks_skus, upsert_k8s_versions, upsert_vm_skus

# On Windows, az is a .cmd batch wrapper — use shell=True so cmd.exe can resolve it.
_ON_WINDOWS = sys.platform == "win32"


def _az_exe() -> str | None:
    """Return the az executable name if found on PATH, else None."""
    for candidate in (["az.cmd", "az"] if _ON_WINDOWS else ["az"]):
        if shutil.which(candidate):
            return candidate
    return None


def _run_az(*args) -> list | dict | None:
    exe = _az_exe()
    if exe is None:
        print(
            "[warn] 'az' CLI not found. Install Azure CLI (https://aka.ms/installazurecli) "
            "and ensure it is on your PATH.",
            file=sys.stderr,
        )
        return None

    cmd = [exe, *args, "-o", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                shell=_ON_WINDOWS)
        if result.returncode != 0:
            print(f"[warn] az {' '.join(args)} failed: {result.stderr.strip()}", file=sys.stderr)
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print(f"[warn] az {' '.join(args)} timed out.", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"[warn] az CLI returned invalid JSON: {e}", file=sys.stderr)
        return None


def refresh_location(conn: sqlite3.Connection, subscription_id: str, location: str):
    loc = location.lower()
    print(f"Refreshing SKU cache for {loc} (subscription {subscription_id})...")

    # 1. VM SKUs available in this subscription + location
    data = _run_az(
        "vm", "list-skus",
        "--location", loc,
        "--resource-type", "virtualMachines",
        "--subscription", subscription_id,
    )
    if data:
        skus = []
        for item in data:
            if item.get("resourceType") != "virtualMachines":
                continue
            restrictions = item.get("restrictions") or []
            if any(r.get("reasonCode") == "NotAvailableForSubscription" for r in restrictions):
                continue
            caps = {c["name"]: c["value"] for c in (item.get("capabilities") or [])}
            try:
                vcpus = int(caps.get("vCPUs", 0))
                mem_mb = int(float(caps.get("MemoryGB", 0)) * 1024)
                if vcpus > 0 and mem_mb > 0:
                    skus.append({"name": item["name"], "vcpus": vcpus, "memory_mb": mem_mb})
            except (ValueError, TypeError):
                pass
        upsert_vm_skus(conn, subscription_id, loc, skus)
        print(f"  Cached {len(skus)} VM SKUs for {loc}.")

    # 2. AKS-compatible node VM sizes for this location
    data = _run_az("aks", "nodepool", "list-skus", "--location", loc)
    if data:
        aks_names = []
        for item in data:
            # Output is a list of objects; name field holds the VM size name
            name = item.get("name") or item.get("vmSize") or item.get("resourceType")
            if name and "/" not in name:  # skip resourceType strings like "managedClusters/agentPools"
                aks_names.append(name)
        upsert_aks_skus(conn, loc, aks_names)
        print(f"  Cached {len(aks_names)} AKS-compatible node SKUs for {loc}.")

    # 3. Supported k8s versions for this location
    data = _run_az("aks", "get-versions", "--location", loc)
    if data:
        versions = []
        # Handle both old format {orchestrators:[]} and new format {values:[]}
        items = data.get("orchestrators") or data.get("values") or []
        for orch in items:
            ver = orch.get("orchestratorVersion") or orch.get("version")
            if ver:
                versions.append({
                    "version": ver,
                    "is_preview": bool(orch.get("isPreview", False)),
                })
        upsert_k8s_versions(conn, loc, versions)
        print(f"  Cached {len(versions)} k8s versions for {loc}.")
