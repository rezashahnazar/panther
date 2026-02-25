import asyncio
from typing import ClassVar
from unittest import IsolatedAsyncioTestCase

from panther import Panther
from panther.app import API, GenericAPI
from panther.configs import config
from panther.request import Request
from panther.test import APIClient


class RequestIsolationRaceAuth:
    seen_paths: ClassVar[set[str]] = set()
    second_request_seen: ClassVar[asyncio.Event | None] = None

    @classmethod
    def reset(cls) -> None:
        cls.seen_paths.clear()
        cls.second_request_seen = asyncio.Event()

    async def __call__(self, request: Request) -> str:
        cls = type(self)
        if cls.second_request_seen is None:
            cls.second_request_seen = asyncio.Event()

        cls.seen_paths.add(request.path)
        if request.path.endswith('/second/'):
            cls.second_request_seen.set()
        if request.path.endswith('/first/'):
            await cls.second_request_seen.wait()
            await asyncio.sleep(0)
        return 'user'


@API(auth=RequestIsolationRaceAuth)
async def function_api_race_endpoint(name: str, request: Request):
    return {'name': name, 'path': request.path}


@API()
async def current_request_endpoint(request: Request):
    current_request = Request.current()
    return {'same_object': current_request is request, 'path': current_request.path}


class ClassBasedRaceEndpoint(GenericAPI):
    auth = RequestIsolationRaceAuth

    async def get(self, name: str, request: Request):
        return {'name': name, 'path': request.path}


test_urls = {
    'race/<name>/': function_api_race_endpoint,
    'race-class/<name>/': ClassBasedRaceEndpoint,
    'current-request': current_request_endpoint,
}


class TestAPIRequestIsolation(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app = Panther(__name__, configs=__name__, urls=test_urls)
        cls.client = APIClient(app=app)

    @classmethod
    def tearDownClass(cls) -> None:
        config.refresh()

    async def test_function_api_keeps_request_isolation_under_concurrency(self):
        RequestIsolationRaceAuth.reset()
        first_response, second_response = await asyncio.gather(
            self.client.get('race/first/'),
            self.client.get('race/second/'),
        )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert first_response.data == {'name': 'first', 'path': '/race/first/'}
        assert second_response.data == {'name': 'second', 'path': '/race/second/'}

    async def test_class_based_apis_keep_request_isolation(self):
        RequestIsolationRaceAuth.reset()
        first_response, second_response = await asyncio.gather(
            self.client.get('race-class/first/'),
            self.client.get('race-class/second/'),
        )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert first_response.data == {'name': 'first', 'path': '/race-class/first/'}
        assert second_response.data == {'name': 'second', 'path': '/race-class/second/'}

    async def test_request_current_returns_active_request(self):
        response = await self.client.get('current-request')
        assert response.status_code == 200
        assert response.data == {'same_object': True, 'path': '/current-request'}

    async def test_request_current_raises_outside_request_context(self):
        with self.assertRaises(LookupError):
            Request.current()

    async def test_request_current_is_reset_after_response(self):
        response = await self.client.get('current-request')
        assert response.status_code == 200

        with self.assertRaises(LookupError):
            Request.current()
