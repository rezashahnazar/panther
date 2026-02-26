import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import pytest

from panther import Panther
from panther.configs import config
from panther.db import Model
from panther.db.connections import db


class RaceBook(Model):
    name: str


@pytest.mark.mongodb
class TestFindOneOrInsertRaceMongoDB(IsolatedAsyncioTestCase):
    DB_NAME = 'test_find_one_or_insert_race'

    @classmethod
    def setUpClass(cls) -> None:
        global DATABASE
        DATABASE = {
            'engine': {
                'class': 'panther.db.connections.MongoDBConnection',
                'host': f'mongodb://127.0.0.1:27017/{cls.DB_NAME}',
            },
        }

    def setUp(self):
        Panther(__name__, configs=__name__, urls={})

    def tearDown(self) -> None:
        db.session.drop_collection('RaceBook')

    @classmethod
    def tearDownClass(cls) -> None:
        config.refresh()

    async def test_find_one_or_insert_with_unique_index_returns_single_record_under_concurrency(self):
        await db.session['RaceBook'].create_index('name', unique=True)

        concurrent_calls = 8
        target_name = 'one-book-only'
        initial_find_calls = 0
        release_initial_finds = asyncio.Event()
        find_lock = asyncio.Lock()
        original_find_one = RaceBook.find_one

        async def controlled_find_one(cls, _filter=None, /, **kwargs):
            nonlocal initial_find_calls

            should_force_initial_none = False
            async with find_lock:
                if initial_find_calls < concurrent_calls:
                    initial_find_calls += 1
                    should_force_initial_none = True
                    if initial_find_calls == concurrent_calls:
                        release_initial_finds.set()

            if should_force_initial_none:
                await release_initial_finds.wait()
                return None

            return await original_find_one(_filter, **kwargs)

        with patch.object(RaceBook, 'find_one', new=classmethod(controlled_find_one)):
            results = await asyncio.gather(
                *[RaceBook.find_one_or_insert(name=target_name) for _ in range(concurrent_calls)],
            )

        inserted_count = sum(1 for _, is_inserted in results if is_inserted)
        returned_ids = {obj.id for obj, _ in results}

        assert inserted_count == 1
        assert len(returned_ids) == 1
        assert await RaceBook.count(name=target_name) == 1
