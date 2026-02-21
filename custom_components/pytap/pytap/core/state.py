"""State management for the pytap parser.

Contains SlotClock (time synchronisation), NodeTableBuilder (accumulates
node table pages), and PersistentState (in-memory infrastructure state).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .types import (
    SlotCounter,
    LongAddress,
    NodeAddress,
    SLOTS_PER_EPOCH,
)


# ---------------------------------------------------------------------------
#  SlotClock
# ---------------------------------------------------------------------------


class SlotClock:
    """Maps SlotCounter values to wall-clock datetime objects.

    Maintains a 48-entry lookup table (4 epochs x 12 indices per epoch),
    each covering a 1000-slot block (~5 seconds at 5ms/slot).
    """

    NOMINAL_MS_PER_SLOT = 5.0
    SLOTS_PER_INDEX = 1000
    NUM_INDICES = 48  # 4 epochs x 12 indices each

    def __init__(self, slot_counter: SlotCounter, time: datetime):
        self._times: list[Optional[datetime]] = [None] * self.NUM_INDICES
        self._last_index: int = -1
        self._last_time: datetime = time
        self._initialize(slot_counter, time)

    @staticmethod
    def _index_and_offset(sc: SlotCounter) -> tuple[int, timedelta]:
        """Compute lookup index and time offset within the block."""
        absolute_slot = sc.epoch * SLOTS_PER_EPOCH + sc.slot_number
        index = absolute_slot // 1000
        offset = timedelta(milliseconds=5.0 * (absolute_slot % 1000))
        return index, offset

    def _initialize(self, sc: SlotCounter, time: datetime):
        """Initialize by computing one reference point and backfilling."""
        index, offset = self._index_and_offset(sc)
        base = time - offset
        self._times[index] = base
        for i in range(1, self.NUM_INDICES):
            prev = (index - i) % self.NUM_INDICES
            self._times[prev] = base - timedelta(milliseconds=5000.0 * i)
        self._last_index = index
        self._last_time = time

    def set(self, sc: SlotCounter, time: datetime):
        """Update the clock with a new observed (SlotCounter, wall-time) pair."""
        if time < self._last_time:
            self._initialize(sc, time)
            return
        index, offset = self._index_and_offset(sc)
        self._times[index] = time - offset
        # Backfill intermediate indices with nominal timing
        if index != self._last_index:
            steps = (index - self._last_index) % self.NUM_INDICES
            for i in range(1, steps):
                fill_idx = (self._last_index + i) % self.NUM_INDICES
                self._times[fill_idx] = self._times[self._last_index] + timedelta(
                    milliseconds=5000.0 * i
                )
        self._last_index = index
        self._last_time = time

    def get(self, sc: SlotCounter) -> datetime:
        """Get the wall-clock time for a given slot counter."""
        index, offset = self._index_and_offset(sc)
        base = self._times[index]
        if base is None:
            # Fallback: use last known time
            return self._last_time
        return base + offset


# ---------------------------------------------------------------------------
#  NodeTableBuilder
# ---------------------------------------------------------------------------


class NodeTableBuilder:
    """Accumulates node table pages until completion.

    Pages are pushed incrementally. An empty page signals that the
    full table has been received.
    """

    def __init__(self):
        self._entries: dict[int, LongAddress] = {}

    def push(
        self,
        start_address: NodeAddress,
        entries: list[tuple[NodeAddress, LongAddress]],
    ) -> Optional[dict[int, LongAddress]]:
        """Add a page. Returns the complete table when an empty page arrives."""
        if len(entries) == 0:
            result = dict(self._entries)
            self._entries.clear()
            return result if result else None
        for node_addr, long_addr in entries:
            self._entries[node_addr.value] = long_addr
        return None


# ---------------------------------------------------------------------------
#  PersistentState
# ---------------------------------------------------------------------------


@dataclass
class PersistentState:
    """In-memory infrastructure state: gateway identities, versions, and node tables.

    Persistence is handled externally by the HA coordinator via to_dict/from_dict.
    """

    gateway_identities: dict[int, LongAddress]
    gateway_versions: dict[int, str]
    gateway_node_tables: dict[int, dict[int, LongAddress]]

    def __init__(self):
        self.gateway_identities = {}
        self.gateway_versions = {}
        self.gateway_node_tables = {}

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict for HA Store."""
        return {
            "gateway_identities": {
                str(k): str(v) for k, v in self.gateway_identities.items()
            },
            "gateway_versions": {str(k): v for k, v in self.gateway_versions.items()},
            "gateway_node_tables": {
                str(gw): {str(nid): str(addr) for nid, addr in nodes.items()}
                for gw, nodes in self.gateway_node_tables.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PersistentState":
        """Deserialise from a dict (as stored in HA Store)."""
        state = cls()
        for k, v in data.get("gateway_identities", {}).items():
            state.gateway_identities[int(k)] = LongAddress.from_str(v)
        for k, v in data.get("gateway_versions", {}).items():
            state.gateway_versions[int(k)] = v
        for gw, nodes in data.get("gateway_node_tables", {}).items():
            state.gateway_node_tables[int(gw)] = {
                int(nid): LongAddress.from_str(addr) for nid, addr in nodes.items()
            }
        return state
