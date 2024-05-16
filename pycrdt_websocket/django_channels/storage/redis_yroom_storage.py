import time
from typing import Optional

import redis.asyncio as redis
from pycrdt import Doc
from redis.exceptions import ResponseError

from .base_yroom_storage import BaseYRoomStorage


class RedisYRoomStorage(BaseYRoomStorage):
    """A YRoom storage that uses Redis as main storage, without
    persistent storage.
    Args:
        room_name: The name of the room.
    """

    def __init__(
        self,
        room_name: str,
        save_throttle_interval: int | None = None,
    ):
        super().__init__(room_name)

        self.save_throttle_interval = save_throttle_interval
        self.last_saved_at = time.time()

        self.redis_key = f"document:{self.room_name}"
        self.redis = self.make_redis()

    async def get_document(self) -> Doc:
        async with self.redis.lock(self.redis_key + ":lock", timeout=5):
            return await self._get_document()

    async def update_document(
        self,
        update: bytes,
    ) -> None:
        await self._add_update_to_queue(update)
        await self._throttled_save_snapshot()

    async def load_snapshot(self) -> Optional[bytes]:
        return None

    async def save_snapshot(self) -> None:
        """Stores the current snapshot of the document.

        This method should employ a Redis lock to retrieve and save the document, ensuring that no other clients can access the document during this operation.
        """  # noqa: E501

        return None

    def make_redis(self):
        """Makes a Redis client.
        Defaults to a local client"""

        return redis.Redis(host="localhost", port=6379, db=0)

    async def close(self):
        await self.save_snapshot()
        await self.redis.close()

    async def _get_document(self, *, delete_updates_after_retrieve=False) -> Doc:
        document = Doc()

        snapshot = await self.load_snapshot()
        snapshot_updates = await self._get_updates(
            delete_updates_after_retrieve=delete_updates_after_retrieve,
        )

        if snapshot:
            document.apply_update(snapshot)

        for update in snapshot_updates:
            document.apply_update(update)

        return document

    async def _add_update_to_queue(self, update: bytes):
        await self.redis.rpush(self.redis_key + ":updates", update)

    async def _get_updates(
        self,
        *,
        delete_updates_after_retrieve=False,
    ) -> list[bytes]:
        async with self.redis.pipeline() as pipe:
            try:
                await pipe.lrange(self.redis_key + ":updates", 0, -1)
                if delete_updates_after_retrieve:
                    await pipe.delete(self.redis_key + ":updates")

                results = await pipe.execute()

                updates = results[0]
            except ResponseError:
                redis_queue_length = await self.redis.llen(self.redis_key + ":updates")

                batch_size = 10

                for i in range(0, redis_queue_length, batch_size):
                    await pipe.lrange(
                        self.redis_key + ":updates",
                        i,
                        i + batch_size - 1,
                    )

                if delete_updates_after_retrieve:
                    await pipe.delete(self.redis_key + ":updates")

                results = await pipe.execute()

                updates_batches = results[:-1] if delete_updates_after_retrieve else results

                updates = [update for batch in updates_batches for update in batch]

            return updates

    async def _throttled_save_snapshot(self) -> None:
        if (
            not self.save_throttle_interval
            or time.time() - self.last_saved_at <= self.save_throttle_interval
        ):
            return

        await self.save_snapshot()

        self.last_saved_at = time.time()

    def _apply_update_to_document(self, document: Doc, update: bytes) -> bytes:
        document.apply_update(update)

        return document.get_update()
