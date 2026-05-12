# Azure SKU Recommendation Engine

A specialized CLI tool designed to fetch 7-day metric telemetry across different Azure resources and produce intelligent recommendations regarding optimization pathways (downscaling over-provisioned nodes, upscaling starving profiles).

## Features

- **Multi-Resource Coverage**: Optimized strategy mappings supporting:
  - Virtual Machines
  - Azure Kubernetes Clusters (AKS)
  - App Service Plans & Web Apps
  - SQL Databases
  - Storage Accounts
- **Auto-Discovery**: Dynamically queries the full telemetry matrix from Azure Monitor natively.
- **Smart Quota Filtering**: Uses the Azure `ResourceSku` REST interfaces to automatically discard unavailable sizing vectors specific to your tenant's subscription region limits.
- **Retail Pricing Overlays**: Calculates rough financial ROI impact over monthly billing cycles directly against current SKUs.

## Installation

```bash
# Set up a clean environment
python -m venv .venv
.\.venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

## Authentication

The engine implements fallback login chains:
1. Reuses existing terminal cached tokens (`AzureCliCredential`).
2. Triggers active OAuth browser validation sequences if no session is active.

## Usage

Simply run the module against standard cloud resource identifier blocks:

```bash
python main.py /subscriptions/<SUB_ID>/resourceGroups/<RG>/providers/Microsoft.Compute/virtualMachines/<NAME>
```

Alternatively, append standard `--verbose` triggers for granular telemetry summaries.

## Local SKU Cache

The tool maintains a local SQLite database (`sku_cache.db`) in the project root to store Azure SKU specifications, compatibility data, and pricing information. This significantly improves performance for subsequent runs.

- **Storage:** `sku_cache.db` (automatically ignored by git)
- **Refresh:** The cache is automatically refreshed if it's older than 7 days. You can force a refresh using the `--refresh-db` flag.
- **Dependency:** Requires the [Azure CLI](https://aka.ms/installazurecli) to be installed and logged in to populate the cache initially.

## Core Threshold Configuration

Evaluations adhere to fixed guidelines:
- **`UNDERUTILIZED`**: Average data stays below `0.20` of safe target lines.
- **`OVERUTILIZED`**: Threshold passes `0.80` capacity limit constraints.
- **`ARTIFICIAL_BASELINE`**: Current deployments physically double normal operational constraints.
