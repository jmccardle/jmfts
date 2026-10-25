"""The Azure backend's launch options, importable without the Azure SDK installed."""

from __future__ import annotations

DEFAULT_REGIONS = ["eastus2", "westus2", "westus3", "centralus", "eastus", "northcentralus"]


def add_azure_arguments(parser) -> None:
    parser.add_argument(
        "--regions",
        nargs="+",
        default=None,
        help=f"regions to place hosts in, round-robin (default: {' '.join(DEFAULT_REGIONS)})",
    )
    parser.add_argument("--size", default="Standard_D2as_v6", help="VM size")
    parser.add_argument(
        "--spot", action="store_true", help="spot priority, evicted VMs deleted (not deallocated)"
    )
    parser.add_argument(
        "--fallback-sizes",
        nargs="*",
        default=["Standard_D2as_v5", "Standard_D2s_v6", "Standard_D2als_v6", "Standard_F2s_v2"],
        help="sizes to try, in order, when a region refuses --size for capacity",
    )
