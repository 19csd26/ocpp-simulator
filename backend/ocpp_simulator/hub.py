"""Fan-out of live events (message log entries and station state) to the UI."""
import asyncio
from collections import defaultdict, deque
from itertools import count


class Hub:
    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()
        self.logs: dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))
        self._ids = count(1)
        self._dirty: dict[str, object] = {}
        self._flusher: asyncio.Task | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # a stuck browser must never stall the simulator
                self.subscribers.discard(q)

    def log(self, station_id: str, entry: dict) -> dict:
        entry = {"id": next(self._ids), **entry}
        self.logs[station_id].append(entry)
        self.publish({"type": "log", "station_id": station_id, "entry": entry})
        return entry

    def state_changed(self, station) -> None:
        """Coalesce bursts of changes into at most ~4 snapshots per second per station."""
        self._dirty[station.cfg.id] = station
        if self._flusher is None or self._flusher.done():
            try:
                self._flusher = asyncio.get_running_loop().create_task(self._flush())
            except RuntimeError:
                pass

    async def _flush(self) -> None:
        while self._dirty:
            await asyncio.sleep(0.25)
            batch, self._dirty = self._dirty, {}
            for station in batch.values():
                if getattr(station, "removed", False):
                    continue
                self.publish({"type": "state", "station_id": station.cfg.id, "state": station.snapshot()})

    def forget(self, station_id: str) -> None:
        """A removed station must not be resurrected by a snapshot that was already queued."""
        self._dirty.pop(station_id, None)
        self.logs.pop(station_id, None)

    def history(self, station_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
        rows = [e for e in self.logs[station_id] if e["id"] > after_id]
        return rows[-limit:] if not after_id else rows[:limit]
