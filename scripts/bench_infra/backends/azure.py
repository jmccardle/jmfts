"""``--backend azure`` — VMs in a resource group, the store a blob container beside them.

Credentials are the four ``AZURE_*`` variables ``azure.identity.EnvironmentCredential``
reads, for a service principal holding Contributor on ONE resource group
(``$JMFTS_BENCH_RG``, default ``jmfts-burst``). Everything this backend creates is in that
group, so the principal's scope is the blast radius.

    pip install azure-identity azure-mgmt-compute azure-mgmt-network \\
        azure-mgmt-storage azure-mgmt-resource azure-mgmt-resourcegraph azure-mgmt-msi \\
        azure-storage-blob

WHAT A LAUNCH CREATES, per region on first use: a VNet, a subnet and an NSG that admits no
inbound traffic (``bench-net-<region>``). Per host: a VM, its OS disk, a NIC and a Standard
public IP — the IP only because a VM in a new VNet has no default outbound access any more,
and the host must reach PyPI, Hugging Face and the store. All four carry
``deleteOption=Delete``, so deleting the VM deletes them, and all four are tagged
``bench-job`` and ``bench-expires`` so ``sweep`` can find them if something else failed.

HOW A HOST GOES AWAY. The bootstrap (``azure_bootstrap.sh``) ends in ``finish.sh``: when the
runner exits, or at the job's deadline, whichever is first. If a user-assigned identity
named ``$JMFTS_BENCH_IDENTITY`` (default ``jmfts-burst-reaper``) exists in the group, it is
attached and the VM deletes itself through it; that needs a role the backend's own
principal cannot grant (see the README). Without it the VM powers off, which still bills
until ``sweep`` deletes it. Spot VMs are created with ``evictionPolicy=Delete``: a
deallocated spot VM keeps counting against spot quota, and that quota is 3 vCPU a region.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from scripts.bench_infra.backends import Backend
from scripts.bench_infra.backends.azure_args import DEFAULT_REGIONS, add_azure_arguments
from scripts.bench_infra.spec import REPO_ROOT, JobSpec, git_state, job_prefix
from scripts.bench_infra.store import BlobStore

RG_ENV, IDENTITY_ENV = "JMFTS_BENCH_RG", "JMFTS_BENCH_IDENTITY"
CONTAINER = "bench"
IMAGE = {
    "publisher": "Canonical",
    "offer": "ubuntu-24_04-lts",
    "sku": "server",
    # Pinned, not "latest": the flake hunt is only meaningful on identical hosts, and a new
    # image can be published between two launches of one job. Read 2026-10-25.
    "version": "24.04.202610210",
}
PG_IMAGE = "pgvector/pgvector:pg16"
TAG_JOB, TAG_EXPIRES, TAG_ROLE = "bench-job", "bench-expires", "bench-role"
#: Errors that mean "this region cannot give you this size right now", not "you asked wrong".
CAPACITY_ERRORS = (
    "SkuNotAvailable",
    "AllocationFailed",
    "ZonalAllocationFailed",
    "OverconstrainedAllocationRequest",
    "OverconstrainedZonalAllocationRequest",
    "SpotMaxPriceTooLow",
    "OperationNotAllowed",  # quota exceeded arrives as this
    "QuotaExceeded",
    "InvalidTemplateDeployment",
)


#: Errors that mean "try the same thing again in a moment". Ten VMs attaching one
#: user-assigned identity at once meets the first of these.
TRANSIENT_ERRORS = ("ConflictingConcurrentWriteNotAllowed", "RetryableError", "TooManyRequests")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AzureBackend(Backend):
    name = "azure"

    def __init__(self):
        from azure.identity import EnvironmentCredential

        self.subscription = os.environ["AZURE_SUBSCRIPTION_ID"]
        self.rg = os.environ.get(RG_ENV, "jmfts-burst")
        self.credential = EnvironmentCredential()
        self._clients: dict = {}
        self._net_lock = threading.Lock()
        self._store: Optional[BlobStore] = None

    def add_arguments(self, parser):
        add_azure_arguments(parser)

    # -- clients ------------------------------------------------------------------------

    def _client(self, name: str):
        if name not in self._clients:
            if name == "compute":
                from azure.mgmt.compute import ComputeManagementClient as C
            elif name == "network":
                from azure.mgmt.network import NetworkManagementClient as C
            elif name == "storage":
                from azure.mgmt.storage import StorageManagementClient as C
            elif name == "resource":
                from azure.mgmt.resource.resources import ResourceManagementClient as C
            elif name == "msi":
                from azure.mgmt.msi import ManagedServiceIdentityClient as C
            else:
                raise KeyError(name)
            self._clients[name] = C(self.credential, self.subscription)
        return self._clients[name]

    def _rg_location(self) -> str:
        return self._client("resource").resource_groups.get(self.rg).location

    # -- the store ----------------------------------------------------------------------

    @property
    def account(self) -> str:
        """Storage account names are global; this one is derived, so every session finds it."""
        return "jmftsbench" + hashlib.sha1(self.subscription.encode()).hexdigest()[:12]

    def _account_key(self) -> str:
        storage = self._client("storage")
        try:
            keys = storage.storage_accounts.list_keys(self.rg, self.account)
        except Exception as exc:  # noqa: BLE001 — absent on first use
            if "ResourceNotFound" not in str(exc) and "was not found" not in str(exc):
                raise
            print(f"creating storage account {self.account} in {self.rg}")
            storage.storage_accounts.begin_create(
                self.rg,
                self.account,
                {
                    "location": self._rg_location(),
                    "sku": {"name": "Standard_LRS"},
                    "kind": "StorageV2",
                    "tags": {TAG_ROLE: "store"},
                    "properties": {"minimumTlsVersion": "TLS1_2", "allowBlobPublicAccess": False},
                },
            ).result()
            storage.blob_containers.create(self.rg, self.account, CONTAINER, {})
            keys = storage.storage_accounts.list_keys(self.rg, self.account)
        # The new-generation models are Mappings, so `.keys` is the dict method.
        return keys["keys"][0]["value"]

    def _sas_url(self, expires: datetime) -> str:
        from azure.storage.blob import ContainerSasPermissions, generate_container_sas

        sas = generate_container_sas(
            self.account,
            CONTAINER,
            account_key=self._account_key(),
            permission=ContainerSasPermissions(
                read=True, add=True, create=True, write=True, list=True
            ),
            expiry=expires,
            start=_now() - timedelta(minutes=10),
        )
        return f"https://{self.account}.blob.core.windows.net/{CONTAINER}?{sas}"

    def store(self) -> BlobStore:
        if self._store is None:
            self._store = BlobStore(self._sas_url(_now() + timedelta(hours=12)))
        return self._store

    # -- source ---------------------------------------------------------------------------

    def _upload_source(self, spec: JobSpec) -> None:
        """The tree a host runs, as a tarball: tracked plus untracked-but-not-ignored files.

        The working tree rather than ``git archive HEAD``, so a job can run code that is not
        committed yet; ``SOURCE_SHA`` says which commit it was and whether it was dirty, and
        every shard's ``env.json`` repeats it.
        """
        key = job_prefix(spec.job_id) + "src.tar.gz"
        store = self.store()
        if key in store.list(key):
            return
        files = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name in filter(None, files):
                path = REPO_ROOT / name.decode()
                if path.is_file():
                    tar.add(path, arcname=name.decode())
            sha, dirty = git_state()
            stamp = f"{sha} {'dirty' if dirty else 'clean'}\n".encode()
            info = tarfile.TarInfo("SOURCE_SHA")
            info.size = len(stamp)
            tar.addfile(info, io.BytesIO(stamp))
        store.put(key, buf.getvalue())
        print(f"uploaded source ({len(buf.getvalue()) / 1e6:.1f} MB)")

    # -- network ------------------------------------------------------------------------

    def _subnet_id(self, region: str) -> str:
        """``bench-net-<region>``: a VNet, a subnet, an NSG with no inbound rules. Idempotent."""
        network = self._client("network")
        name = f"bench-net-{region}"
        with self._net_lock:
            try:
                return network.subnets.get(self.rg, name, "hosts").id
            except Exception:  # noqa: BLE001 — absent on first use in this region
                pass
            nsg = network.network_security_groups.begin_create_or_update(
                self.rg, name, {"location": region, "tags": {TAG_ROLE: "network"}}
            ).result()
            network.virtual_networks.begin_create_or_update(
                self.rg,
                name,
                {
                    "location": region,
                    "tags": {TAG_ROLE: "network"},
                    "properties": {
                        "addressSpace": {"addressPrefixes": ["10.42.0.0/16"]},
                        "subnets": [
                            {
                                "name": "hosts",
                                "properties": {
                                    "addressPrefix": "10.42.0.0/24",
                                    "networkSecurityGroup": {"id": nsg.id},
                                },
                            }
                        ],
                    },
                },
            ).result()
            return network.subnets.get(self.rg, name, "hosts").id

    def _identity(self) -> Optional[tuple[str, str]]:
        """(resource id, client id) of the self-delete identity, if the owner made one."""
        name = os.environ.get(IDENTITY_ENV, "jmfts-burst-reaper")
        try:
            ident = self._client("msi").user_assigned_identities.get(self.rg, name)
        except Exception:  # noqa: BLE001 — absent unless set up by hand
            return None
        props = getattr(ident, "properties", None)
        client_id = getattr(ident, "client_id", None) or getattr(props, "client_id", None)
        return ident.id, client_id

    # -- launch -------------------------------------------------------------------------

    def _bootstrap(self, spec: JobSpec, host: str, store_url: str, identity) -> str:
        extras = ",".join(spec.host.get("extras", []))
        torch = any(e in ("embed", "dev") for e in spec.host.get("extras", []))
        deadline = datetime.fromisoformat(spec.deadline)
        values = {
            "STORE": store_url,
            "JOB": spec.job_id,
            "HOST": host,
            "IDENTITY_CLIENT_ID": identity[1] if identity else "",
            "NEEDS_POSTGRES": "1" if spec.host.get("postgres") else "0",
            "NEEDS_LIBREOFFICE": "1" if spec.host.get("libreoffice") else "0",
            "EXTRAS": extras,
            "TORCH": "1" if torch else "0",
            "PG_IMAGE": PG_IMAGE,
            "DEADLINE_SECONDS": str(max(60, int((deadline - _now()).total_seconds()))),
        }
        script = (Path(__file__).parent / "azure_bootstrap.sh").read_text()
        for key, value in values.items():
            if "'" in value:
                raise ValueError(f"{key} may not contain a single quote")
            script = script.replace(f"@@{key}@@", value)
        if re.search(r"@@[A-Z_]+@@", script):
            raise ValueError("the bootstrap template has a placeholder nothing filled")
        return script

    def launch(self, spec: JobSpec, hosts: int, args) -> None:
        import base64

        regions = args.regions or DEFAULT_REGIONS
        sizes = [args.size, *[s for s in args.fallback_sizes if s != args.size]]
        self._upload_source(spec)
        deadline = datetime.fromisoformat(spec.deadline)
        store_url = self._sas_url(deadline + timedelta(days=1))
        identity = self._identity()
        if identity is None:
            print("no self-delete identity in the group: finished VMs power off; run `sweep`")

        existing = {vm["name"] for vm in self.hosts(spec.job_id)}
        names = []
        i = 0
        while len(names) < hosts:
            name = f"b-{spec.job_id}-{i:02d}"[:64]
            if name not in existing:
                names.append(name)
            i += 1

        def start(index_name):
            index, name = index_name
            # Each host starts at its own region and walks the list, so one region refusing
            # does not stop the host — it lands in the next region that has capacity.
            order = regions[index % len(regions) :] + regions[: index % len(regions)]
            custom = base64.b64encode(
                self._bootstrap(spec, name, store_url, identity).encode()
            ).decode()
            failures = []
            for region in order:
                for size in sizes:
                    for attempt in range(3):
                        try:
                            self._create_vm(name, region, size, args.spot, custom, spec, identity)
                            return f"{name}: {region} {size}{' spot' if args.spot else ''}"
                        except Exception as exc:  # noqa: BLE001 — classified below
                            code = _error_code(exc)
                            self._delete_vm_resources(name)
                            if code in TRANSIENT_ERRORS and attempt < 2:
                                time.sleep(15 * (attempt + 1))
                                continue
                            failures.append(f"{region}/{size}: {code}")
                            if code not in CAPACITY_ERRORS:
                                return f"{name}: FAILED {code}: {str(exc)[:300]}"
                            break
            return f"{name}: no region had capacity ({'; '.join(failures)})"

        with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
            for line in pool.map(start, enumerate(names)):
                print("  " + line)

    def _create_vm(self, name, region, size, spot, custom_data, spec, identity) -> None:
        network, compute = self._client("network"), self._client("compute")
        tags = {TAG_JOB: spec.job_id, TAG_EXPIRES: spec.deadline, TAG_ROLE: "host"}
        subnet = self._subnet_id(region)
        ip = network.public_ip_addresses.begin_create_or_update(
            self.rg,
            f"{name}-ip",
            {
                "location": region,
                "tags": tags,
                "sku": {"name": "Standard"},
                "properties": {"publicIPAllocationMethod": "Static"},
            },
        ).result()
        nic = network.network_interfaces.begin_create_or_update(
            self.rg,
            f"{name}-nic",
            {
                "location": region,
                "tags": tags,
                "properties": {
                    "ipConfigurations": [
                        {
                            "name": "ip",
                            "properties": {
                                "subnet": {"id": subnet},
                                "publicIPAddress": {
                                    "id": ip.id,
                                    "properties": {"deleteOption": "Delete"},
                                },
                            },
                        }
                    ]
                },
            },
        ).result()
        properties: dict = {
            "hardwareProfile": {"vmSize": size},
            "storageProfile": {
                "imageReference": IMAGE,
                "osDisk": {
                    "createOption": "FromImage",
                    "diskSizeGB": 64,
                    "managedDisk": {"storageAccountType": "StandardSSD_LRS"},
                    "deleteOption": "Delete",
                },
                # The v6 sizes are NVMe-only; the older ones are SCSI-only.
                "diskControllerType": "NVMe" if size.endswith("v6") else "SCSI",
            },
            "osProfile": {
                "computerName": name[:63],
                "adminUsername": "bench",
                "customData": custom_data,
                "linuxConfiguration": {
                    "disablePasswordAuthentication": True,
                    # No inbound rule admits SSH; the key exists because Azure requires one.
                    "ssh": {
                        "publicKeys": [
                            {
                                "path": "/home/bench/.ssh/authorized_keys",
                                "keyData": _throwaway_ssh_key(),
                            }
                        ]
                    },
                },
            },
            "networkProfile": {
                "networkInterfaces": [{"id": nic.id, "properties": {"deleteOption": "Delete"}}]
            },
        }
        if spot:
            properties.update(
                priority="Spot", evictionPolicy="Delete", billingProfile={"maxPrice": -1}
            )
        vm: dict = {"location": region, "tags": tags, "properties": properties}
        if identity:
            vm["identity"] = {"type": "UserAssigned", "userAssignedIdentities": {identity[0]: {}}}
        compute.virtual_machines.begin_create_or_update(self.rg, name, vm).result()

    def _delete_vm_resources(self, name: str) -> None:
        """Remove a half-created host: the VM if any, then its NIC and IP if they survived."""
        compute, network = self._client("compute"), self._client("network")
        for delete in (
            lambda: compute.virtual_machines.begin_delete(
                self.rg, name, force_deletion=True
            ).result(),
            lambda: network.network_interfaces.begin_delete(self.rg, f"{name}-nic").result(),
            lambda: network.public_ip_addresses.begin_delete(self.rg, f"{name}-ip").result(),
        ):
            try:
                delete()
            except Exception:  # noqa: BLE001 — absent is fine; sweep catches the rest
                pass

    # -- watching and cleaning up -------------------------------------------------------

    def hosts(self, job_id: str) -> list[dict]:
        compute = self._client("compute")
        out = []
        for vm in compute.virtual_machines.list(self.rg):
            if (vm.tags or {}).get(TAG_JOB) != job_id:
                continue
            out.append(
                {
                    "name": vm.name,
                    "location": vm.location,
                    "state": self._power_state(vm.name),
                    "tags": vm.tags,
                }
            )
        return out

    def _power_state(self, name: str) -> str:
        try:
            view = self._client("compute").virtual_machines.instance_view(self.rg, name)
        except Exception:  # noqa: BLE001 — deleted between list and view
            return "gone"
        codes = [s.code for s in view.statuses or []]
        power = next((c.split("/", 1)[1] for c in codes if c.startswith("PowerState/")), None)
        return power or ",".join(codes)

    def teardown(self, job_id: str) -> None:
        names = [vm["name"] for vm in self.hosts(job_id)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self._delete_vm_resources, names))
        print(f"deleted {len(names)} VM(s) for {job_id}")
        self._sweep_orphans(lambda tags: tags.get(TAG_JOB) == job_id)

    def sweep(self, *, dry_run: bool, include_finished: bool) -> None:
        """Delete hosts past their deadline, and (default) hosts whose runner has finished."""
        now = _now()
        compute = self._client("compute")
        doomed = []
        for vm in compute.virtual_machines.list(self.rg):
            tags = vm.tags or {}
            if tags.get(TAG_ROLE) != "host":
                continue
            reason = None
            expires = tags.get(TAG_EXPIRES)
            if expires and datetime.fromisoformat(expires) < now:
                reason = f"expired {expires}"
            elif include_finished:
                state = self._power_state(vm.name)
                if state in ("stopped", "deallocated"):
                    reason = f"power state {state}"
                else:
                    record = self.store().get(
                        job_prefix(tags.get(TAG_JOB, "")) + f"hosts/{vm.name}.json"
                    )
                    if record and json.loads(record).get("state") == "finished":
                        reason = "runner finished"
            if reason:
                doomed.append(vm.name)
                print(
                    f"{'would delete' if dry_run else 'deleting'} {vm.name} ({vm.location}): {reason}"
                )
        if not dry_run:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(self._delete_vm_resources, doomed))
            self._sweep_orphans(lambda tags: tags.get(TAG_ROLE) == "host")
        print(f"{len(doomed)} VM(s) {'to delete' if dry_run else 'deleted'}")

    def _sweep_orphans(self, match) -> None:
        """NICs, public IPs and disks a host left behind when its VM never came up."""
        network, compute = self._client("network"), self._client("compute")
        live = {vm.name for vm in compute.virtual_machines.list(self.rg)}
        for nic in network.network_interfaces.list(self.rg):
            if match(nic.tags or {}) and nic.name.removesuffix("-nic") not in live:
                network.network_interfaces.begin_delete(self.rg, nic.name).result()
                print(f"deleted orphan NIC {nic.name}")
        for ip in network.public_ip_addresses.list(self.rg):
            if (
                match(ip.tags or {})
                and ip.name.removesuffix("-ip") not in live
                and _ip_unattached(ip)
            ):
                network.public_ip_addresses.begin_delete(self.rg, ip.name).result()
                print(f"deleted orphan public IP {ip.name}")
        for disk in compute.disks.list_by_resource_group(self.rg):
            if disk.managed_by is None and (disk.tags or {}).get(TAG_ROLE) in ("host", None):
                if any(disk.name.startswith(n) for n in live):
                    continue
                if disk.name.startswith("b-"):
                    compute.disks.begin_delete(self.rg, disk.name).result()
                    print(f"deleted orphan disk {disk.name}")

    # -- regions ------------------------------------------------------------------------

    def report_regions(self, size: str, regions: Optional[list[str]]) -> None:
        """Quota, spot price and eviction bracket for ``size``, one region a line.

        Quota from the compute usage API; price and eviction rate from Resource Graph's
        ``SpotResources`` table, queried at TENANT scope — the principal is scoped to one
        resource group, and a subscription-scoped query of that table comes back empty.
        """
        from azure.mgmt.resourcegraph import ResourceGraphClient
        from azure.mgmt.resourcegraph.models import QueryRequest

        regions = regions or DEFAULT_REGIONS + [
            "westus",
            "westcentralus",
            "canadacentral",
            "northeurope",
            "uksouth",
            "swedencentral",
        ]
        graph = ResourceGraphClient(self.credential)
        low = size.lower()
        in_list = ",".join(f"'{r}'" for r in regions)
        rates = {
            row["location"]: row["rate"]
            for row in graph.resources(
                QueryRequest(
                    query=(
                        "SpotResources | where type =~ 'microsoft.compute/skuspotevictionrate/location'"
                        f" | where tolower(sku.name) == '{low}' | where location in ({in_list})"
                        " | project location, rate=tostring(properties.evictionRate)"
                    )
                )
            ).data
        }
        prices = {
            row["location"]: row["price"]
            for row in graph.resources(
                QueryRequest(
                    query=(
                        "SpotResources | where type =~ 'microsoft.compute/skuspotpricehistory/ostype/location'"
                        f" | where tolower(sku.name) == '{low}' and properties.osType =~ 'linux'"
                        f" | where location in ({in_list})"
                        " | project location, price=todouble(properties.spotPrices[0].priceUSD)"
                    )
                )
            ).data
        }
        compute = self._client("compute")
        print(
            f"{size}: region, regular vCPU used/limit, spot vCPU used/limit, spot $/h, eviction %"
        )
        for region in regions:
            usage = {u.name.value: (u.current_value, u.limit) for u in compute.usage.list(region)}
            cores, spot = usage.get("cores", ("?", "?")), usage.get("lowPriorityCores", ("?", "?"))
            price = prices.get(region)
            print(
                f"  {region:16s} {cores[0]}/{cores[1]:<4} {spot[0]}/{spot[1]:<4} "
                f"{price if price is None else f'{price:.4f}':>8}  {rates.get(region, '?')}"
            )


def _ip_unattached(ip) -> bool:
    props = getattr(ip, "properties", None)
    return getattr(props, "ip_configuration", getattr(ip, "ip_configuration", None)) is None


def _error_code(exc: Exception) -> str:
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None)
    if code:
        return code
    text = str(exc)
    for known in CAPACITY_ERRORS:
        if known in text:
            return known
    return type(exc).__name__


_SSH_KEY: Optional[str] = None


def _throwaway_ssh_key() -> str:
    """An RSA public key whose private half is never kept. Azure requires a key on a Linux
    VM; nothing here logs in, and no NSG rule would let anything try."""
    global _SSH_KEY
    if _SSH_KEY is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        _SSH_KEY = (
            key.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
    return _SSH_KEY
