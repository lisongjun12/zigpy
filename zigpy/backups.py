"""Classes to interact with zigpy network backups, including JSON serialization."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING, Any

import zigpy.config as conf
import zigpy.state
import zigpy.types as t
from zigpy.util import ListenableMixin

if TYPE_CHECKING:
    import zigpy.application

LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class NetworkBackup(zigpy.state.BaseDataclassMixin):
    backup_time: datetime = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    network_info: zigpy.state.NetworkInfo = dataclasses.field(
        default_factory=zigpy.state.NetworkInfo
    )
    node_info: zigpy.state.NodeInfo = dataclasses.field(
        default_factory=zigpy.state.NodeInfo
    )

    def is_compatible_with(self, backup: NetworkBackup) -> bool:
        """
        Two backups are compatible if, ignoring frame counters, the same external device
        will be able to join either network.
        """

        return (
            self.node_info == backup.node_info
            and self.network_info.extended_pan_id == backup.network_info.extended_pan_id
            and self.network_info.pan_id == backup.network_info.pan_id
            and self.network_info.nwk_update_id == backup.network_info.nwk_update_id
            and self.network_info.nwk_manager_id == backup.network_info.nwk_manager_id
            and self.network_info.channel == backup.network_info.channel
            and self.network_info.security_level == backup.network_info.security_level
            and self.network_info.tc_link_key.key == backup.network_info.tc_link_key.key
            and self.network_info.network_key.key == backup.network_info.network_key.key
        )

    def supersedes(self, backup: NetworkBackup) -> bool:
        """
        Checks if this network backup is more recent than another backup.
        """

        return (
            self.is_compatible_with(backup)
            and (
                self.network_info.network_key.tx_counter
                > backup.network_info.network_key.tx_counter
            )
            and self.network_info.nwk_update_id >= backup.network_info.nwk_update_id
        )

    def is_complete(self) -> bool:
        """
        Checks if this backup captures enough network state to recreate the network.
        """

        return (
            self.node_info.ieee != t.EUI64.UNKNOWN
            and self.network_info.extended_pan_id != t.EUI64.UNKNOWN
            and self.network_info.pan_id not in (0x0000, 0xFFFF)
            and self.network_info.channel in range(11, 26 + 1)
            and self.network_info.network_key.key != t.KeyData.UNKNOWN
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_time": self.backup_time.isoformat(),
            "network_info": self.network_info.as_dict(),
            "node_info": self.node_info.as_dict(),
        }

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> NetworkBackup:
        if "metadata" in obj:
            return cls.from_open_coordinator_json(obj)
        elif "network_info" in obj:
            return cls(
                backup_time=datetime.fromisoformat(obj["backup_time"]),
                network_info=zigpy.state.NetworkInfo.from_dict(obj["network_info"]),
                node_info=zigpy.state.NodeInfo.from_dict(obj["node_info"]),
            )
        else:
            raise ValueError(f"Invalid network backup object: {obj!r}")

    def as_open_coordinator_json(self) -> dict[str, Any]:
        return _network_backup_to_open_coordinator_backup(self)

    @classmethod
    def from_open_coordinator_json(cls, obj: dict[str, Any]) -> NetworkBackup:
        return _open_coordinator_backup_to_network_backup(obj)


class BackupManager(ListenableMixin):
    def __init__(self, app: zigpy.application.ControllerApplication):
        super().__init__()

        self.app: zigpy.application.ControllerApplication = app
        self.backups: list[NetworkBackup] = []

        self._backup_task: asyncio.Task | None = None

    async def create_backup(self, *, load_devices: bool = False) -> NetworkBackup:
        await self.app.load_network_info(load_devices=load_devices)

        # Creation time will automatically be set
        backup = NetworkBackup(
            network_info=self.app.state.network_info,
            node_info=self.app.state.node_info,
        )

        self.add_backup(backup)

        return backup

    async def restore_backup(
        self, backup: NetworkBackup, counter_increment: int = 10000
    ) -> None:
        if not backup.is_complete():
            raise ValueError("Backup is incomplete, it is not possible to restore")

        key = backup.network_info.network_key
        new_backup = NetworkBackup(
            network_info=backup.network_info.replace(
                network_key=key.replace(tx_counter=key.tx_counter + counter_increment)
            ),
            node_info=backup.node_info,
        )

        await self.app.write_network_info(
            network_info=new_backup.network_info,
            node_info=new_backup.node_info,
        )

        await self.create_backup()

    def add_backup(self, backup: NetworkBackup):
        """
        Adds a new backup to the database, superseding older ones if necessary.
        """

        LOGGER.debug("Adding a new backup %s", backup)

        if not backup.is_complete():
            LOGGER.debug("Backup is incomplete, ignoring")
            return

        for index, old_backup in enumerate(self.backups):
            if not backup.is_compatible_with(old_backup):
                continue
            elif old_backup.supersedes(backup):
                # Ignore this backup if it's superseded by an old backup. This should
                # not happen during normal operation, only if an old backup is
                # intentionally restored.
                LOGGER.debug("Backup is superseded by %s, ignoring", old_backup)
                return

            # Replace the old backup with our more recent one, since we either supersede
            # it or are superficially identical to it
            LOGGER.debug("Replacing %s", old_backup)
            self.listener_event("network_backup_removed", old_backup)
            self.listener_event("network_backup_created", backup)
            self.backups[index] = backup

            break
        else:
            # If no backup was replaced, create a new one
            self.listener_event("network_backup_created", backup)
            self.backups.append(backup)

        self.backups.sort(reverse=True, key=lambda backup: backup.backup_time)

    def start_periodic_backups(self, period: int | float) -> None:
        self.stop_periodic_backups()
        self._backup_task = asyncio.create_task(self._backup_loop(period))

    def stop_periodic_backups(self):
        if self._backup_task is not None:
            self._backup_task.cancel()

    async def _backup_loop(self, period: int | float):
        while True:
            try:
                await self.create_backup()
            except Exception:
                LOGGER.warning("Failed to create a network backup", exc_info=True)

            LOGGER.debug("Waiting for %ss before backing up again", period)
            await asyncio.sleep(period)

    def __getitem__(self, key) -> NetworkBackup:
        return self.backups[key]


def _network_backup_to_open_coordinator_backup(backup: NetworkBackup) -> dict[str, Any]:
    """
    Converts a `NetworkBackup` to an Open Coordinator Backup-compatible dictionary.
    """

    node_info = backup.node_info
    network_info = backup.network_info

    devices = {}

    for ieee, nwk in network_info.nwk_addresses.items():
        devices[ieee] = {
            "ieee_address": ieee.serialize()[::-1].hex(),
            "nwk_address": nwk.serialize()[::-1].hex(),
            "is_child": False,
        }

    for ieee in network_info.children:
        if ieee not in devices:
            devices[ieee] = {
                "ieee_address": ieee.serialize()[::-1].hex(),
                "nwk_address": None,
                "is_child": True,
            }
        else:
            devices[ieee]["is_child"] = True

    for key in network_info.key_table:
        if key.partner_ieee not in devices:
            devices[key.partner_ieee] = {
                "ieee_address": key.partner_ieee.serialize()[::-1].hex(),
                "nwk_address": None,
                "is_child": False,
            }

        devices[key.partner_ieee]["link_key"] = {
            "key": key.key.serialize().hex(),
            "tx_counter": key.tx_counter,
            "rx_counter": key.rx_counter,
        }

    return {
        "metadata": {
            "version": 1,
            "format": "zigpy/open-coordinator-backup",
            "source": network_info.source,
            "internal": {
                "creation_time": backup.backup_time.isoformat(),
                "node": {
                    "ieee": node_info.ieee.serialize()[::-1].hex(),
                    "nwk": node_info.nwk.serialize()[::-1].hex(),
                    "type": zigpy.state.LOGICAL_TYPE_TO_JSON[node_info.logical_type],
                },
                "network": {
                    "tc_link_key": {
                        "key": network_info.tc_link_key.key.serialize().hex(),
                        "frame_counter": network_info.tc_link_key.tx_counter,
                    },
                    "tc_address": network_info.tc_link_key.partner_ieee.serialize()[
                        ::-1
                    ].hex(),
                    "nwk_manager": network_info.nwk_manager_id.serialize()[::-1].hex(),
                },
                "link_key_seqs": {
                    key.partner_ieee.serialize()[::-1].hex(): key.seq
                    for key in network_info.key_table
                },
                **network_info.metadata,
            },
        },
        "stack_specific": network_info.stack_specific,
        "coordinator_ieee": node_info.ieee.serialize()[::-1].hex(),
        "pan_id": network_info.pan_id.serialize()[::-1].hex(),
        "extended_pan_id": network_info.extended_pan_id.serialize()[::-1].hex(),
        "nwk_update_id": network_info.nwk_update_id,
        "security_level": network_info.security_level,
        "channel": network_info.channel,
        "channel_mask": list(network_info.channel_mask),
        "network_key": {
            "key": network_info.network_key.key.serialize().hex(),
            "sequence_number": network_info.network_key.seq or 0,
            "frame_counter": network_info.network_key.tx_counter or 0,
        },
        "devices": sorted(devices.values(), key=lambda d: d["ieee_address"]),
    }


def _open_coordinator_backup_to_network_backup(obj: dict[str, Any]) -> NetworkBackup:
    """
    Creates a `NetworkBackup` from an Open Coordinator Backup dictionary.
    """

    internal = obj["metadata"].get("internal", {})

    node_info = zigpy.state.NodeInfo()
    node_meta = internal.get("node", {})

    if "nwk" in node_meta:
        node_info.nwk, _ = t.NWK.deserialize(bytes.fromhex(node_meta["nwk"])[::-1])
    else:
        node_info.nwk = t.NWK(0x0000)

    node_info.logical_type = zigpy.state.JSON_TO_LOGICAL_TYPE[
        node_meta.get("type", "coordinator")
    ]

    # Should be identical to `metadata.internal.node.ieee`
    node_info.ieee, _ = t.EUI64.deserialize(
        bytes.fromhex(obj["coordinator_ieee"])[::-1]
    )

    network_info = zigpy.state.NetworkInfo()
    network_info.source = obj["metadata"]["source"]
    network_info.metadata = {
        k: v
        for k, v in internal.items()
        if k not in ("node", "network", "link_key_seqs", "creation_time")
    }
    network_info.pan_id, _ = t.NWK.deserialize(bytes.fromhex(obj["pan_id"])[::-1])
    network_info.extended_pan_id, _ = t.EUI64.deserialize(
        bytes.fromhex(obj["extended_pan_id"])[::-1]
    )
    network_info.nwk_update_id = obj["nwk_update_id"]

    network_meta = internal.get("network", {})

    if "nwk_manager" in network_meta:
        network_info.nwk_manager_id, _ = t.NWK.deserialize(
            bytes.fromhex(network_meta["nwk_manager"])
        )
    else:
        network_info.nwk_manager_id = t.NWK(0x0000)

    network_info.channel = obj["channel"]
    network_info.channel_mask = t.Channels.from_channel_list(obj["channel_mask"])
    network_info.security_level = obj["security_level"]

    if obj.get("stack_specific"):
        network_info.stack_specific = obj.get("stack_specific")

    network_info.tc_link_key = zigpy.state.Key()

    if "tc_link_key" in network_meta:
        network_info.tc_link_key.key, _ = t.KeyData.deserialize(
            bytes.fromhex(network_meta["tc_link_key"]["key"])
        )
        network_info.tc_link_key.tx_counter = network_meta["tc_link_key"].get(
            "frame_counter", 0
        )
        network_info.tc_link_key.partner_ieee, _ = t.EUI64.deserialize(
            bytes.fromhex(network_meta["tc_address"])[::-1]
        )
    else:
        network_info.tc_link_key.key = conf.CONF_NWK_TC_LINK_KEY_DEFAULT
        network_info.tc_link_key.partner_ieee = node_info.ieee

    network_info.network_key = zigpy.state.Key()
    network_info.network_key.key, _ = t.KeyData.deserialize(
        bytes.fromhex(obj["network_key"]["key"])
    )
    network_info.network_key.tx_counter = obj["network_key"]["frame_counter"]
    network_info.network_key.seq = obj["network_key"]["sequence_number"]

    network_info.children = []
    network_info.nwk_addresses = {}

    for device in obj["devices"]:
        if device["nwk_address"] is not None:
            nwk, _ = t.NWK.deserialize(bytes.fromhex(device["nwk_address"])[::-1])
        else:
            nwk = None

        ieee, _ = t.EUI64.deserialize(bytes.fromhex(device["ieee_address"])[::-1])

        # The `is_child` key is currently optional
        if device.get("is_child", True):
            network_info.children.append(ieee)

        if nwk is not None:
            network_info.nwk_addresses[ieee] = nwk

        if "link_key" in device:
            key = zigpy.state.Key()
            key.key, _ = t.KeyData.deserialize(bytes.fromhex(device["link_key"]["key"]))
            key.tx_counter = device["link_key"]["tx_counter"]
            key.rx_counter = device["link_key"]["rx_counter"]
            key.partner_ieee = ieee

            try:
                key.seq = obj["metadata"]["internal"]["link_key_seqs"][
                    device["ieee_address"]
                ]
            except KeyError:
                key.seq = 0

            network_info.key_table.append(key)

        # XXX: Devices that are not children, have no NWK address, and have no link key
        #      are effectively ignored, since there is no place to write them

    if "date" in internal:
        # Z2M format
        creation_time = internal["date"].replace("Z", "+00:00")
    else:
        # Zigpy format
        creation_time = internal.get("creation_time", "1970-01-01T00:00:00+00:00")

    return NetworkBackup(
        backup_time=datetime.fromisoformat(creation_time),
        network_info=network_info,
        node_info=node_info,
    )
