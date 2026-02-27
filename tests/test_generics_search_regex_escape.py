import multiprocessing as mp
import re
import time
from typing import ClassVar
from unittest import TestCase

from panther.configs import config
from panther.db.connections import MongoDBConnection
from panther.generics import ListAPI
from panther.request import Request


def _run_regex_search(pattern: str, text: str, repeats: int, done: mp.Queue):
    start = time.perf_counter()
    for _ in range(repeats):
        re.search(pattern, text)
    done.put(time.perf_counter() - start)


class ListAPISearchTest(ListAPI):
    search_fields: ClassVar[list[str]] = ['name']

    async def get_query(self, request: Request, **kwargs):
        raise NotImplementedError


class TestListAPISearchRegexEscape(TestCase):
    REDOS_PATTERN = r'([a-zA-Z]+)*$'
    PAYLOAD_LENGTH_RANGE = range(24, 42)
    ESCAPED_TIMEOUT_SECONDS = 3.0
    RAW_STALL_TIMEOUT_SECONDS = 0.5

    def setUp(self):
        self.previous_database = config.DATABASE
        config.DATABASE = object.__new__(MongoDBConnection)

    def tearDown(self):
        config.DATABASE = self.previous_database

    def test_mongodb_search_pattern_escapes_user_input(self):
        user_input = '(a+)+$'

        protected_pattern = self._extract_mongodb_search_pattern(user_input)

        assert protected_pattern == re.escape(user_input)

    def test_raw_regex_stalls_on_near_miss_payload_but_escaped_pattern_completes(self):
        raw_pattern = self.REDOS_PATTERN
        escaped_pattern = self._extract_mongodb_search_pattern(raw_pattern)

        trigger_length, attempts = self._find_redos_trigger_length(
            raw_pattern=raw_pattern,
            escaped_pattern=escaped_pattern,
        )

        assert trigger_length is not None, f'ReDoS probe did not find a trigger: {attempts}'

        raw_matching_completed, _ = self._run_with_deadline(
            pattern=raw_pattern,
            text='A' * trigger_length,
            repeats=5_000,
            timeout_seconds=self.ESCAPED_TIMEOUT_SECONDS,
        )
        assert raw_matching_completed is True, f'ReDoS probe: {attempts}'

    def _extract_mongodb_search_pattern(self, user_input: str) -> str:
        query = ListAPISearchTest().process_search(query_params={'search': user_input})
        return query['$or'][0]['name']['$regex']

    def _find_redos_trigger_length(self, *, raw_pattern: str, escaped_pattern: str) -> tuple[int | None, list[dict]]:
        attempts: list[dict] = []

        for length in self.PAYLOAD_LENGTH_RANGE:
            near_miss_payload = 'A' * length + '!'
            raw_completed, raw_elapsed = self._run_with_deadline(
                pattern=raw_pattern,
                text=near_miss_payload,
                repeats=1,
                timeout_seconds=self.RAW_STALL_TIMEOUT_SECONDS,
            )
            escaped_completed, escaped_elapsed = self._run_with_deadline(
                pattern=escaped_pattern,
                text=near_miss_payload,
                repeats=5_000,
                timeout_seconds=self.ESCAPED_TIMEOUT_SECONDS,
            )

            attempts.append(
                {
                    'payload_length': length,
                    'raw_completed': raw_completed,
                    'raw_elapsed': raw_elapsed,
                    'escaped_completed': escaped_completed,
                    'escaped_elapsed': escaped_elapsed,
                },
            )

            if raw_completed is False and escaped_completed is True:
                return length, attempts

        return None, attempts

    @staticmethod
    def _run_with_deadline(
        *,
        pattern: str,
        text: str,
        repeats: int,
        timeout_seconds: float,
    ) -> tuple[bool, float | None]:
        done: mp.Queue = mp.Queue()
        process = mp.Process(target=_run_regex_search, args=(pattern, text, repeats, done))
        process.start()
        process.join(timeout=timeout_seconds)

        if process.is_alive():
            process.terminate()
            process.join()
            return False, None

        if process.exitcode != 0:
            raise AssertionError(f'regex worker failed with exit code {process.exitcode}')

        elapsed_seconds = done.get_nowait() if not done.empty() else None
        return True, elapsed_seconds
