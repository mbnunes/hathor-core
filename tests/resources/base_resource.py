from twisted.internet.defer import succeed
from twisted.web import server
from twisted.web.test.requesthelper import DummyRequest

from hathor.manager import HathorManager, TestMode
from hathor.p2p.peer_id import PeerId
from hathor.util import json_dumpb, json_loadb
from tests import unittest


class _BaseResourceTest:
    class _ResourceTest(unittest.TestCase):
        def _manager_kwargs(self):
            peer_id = PeerId()
            network = 'testnet'
            wallet = self._create_test_wallet()
            tx_storage = getattr(self, 'tx_storage', None)
            return dict(
                peer_id=peer_id,
                network=network,
                wallet=wallet,
                tx_storage=tx_storage,
                wallet_index=True
            )

        def setUp(self):
            super().setUp()
            self.reactor = self.clock
            self.manager = HathorManager(self.reactor, **self._manager_kwargs())
            self.manager.allow_mining_without_peers()
            self.manager.test_mode = TestMode.TEST_ALL_WEIGHT
            self.manager.start()

        def tearDown(self):
            return self.manager.stop()


class RequestBody(object):
    """
    Dummy request body object to represent content
    """

    def __init__(self):
        self.content = None

    def setvalue(self, value):
        self.content = value

    def read(self):
        return self.content


class TestDummyRequest(DummyRequest):
    def __init__(self, method, url, args=None, headers=None):
        DummyRequest.__init__(self, url.split('/'))
        self.method = method
        self.headers = headers or {}
        self.content = RequestBody()

        # Set request args
        args = args or {}
        for k, v in args.items():
            self.addArg(k, v)

    def json_value(self):
        return json_loadb(self.written[0])


class StubSite(server.Site):
    def get(self, url, args=None, headers=None):
        return self._request('GET', url, None, args, headers)

    def post(self, url, body=None, args=None, headers=None):
        return self._request('POST', url, body, args, headers)

    def put(self, url, body=None, args=None, headers=None):
        return self._request('PUT', url, body, args, headers)

    def options(self, url, args=None, headers=None):
        return self._request('OPTIONS', url, None, args, headers)

    def _request(self, method, url, body, args, headers):
        request = TestDummyRequest(method, url, args, headers)
        # body content
        if (method == 'POST' or method == 'PUT') and body:
            # Creating post content exactly the same as twisted resource
            request.content.setvalue(json_dumpb(body))

        resource = self.getResourceFor(request)
        result = resource.render(request)
        return self._resolveResult(request, result)

    def _resolveResult(self, request, result):
        if isinstance(result, bytes):
            request.write(result)
            request.finish()
            return succeed(request)
        elif result is server.NOT_DONE_YET:
            if request.finished:
                return succeed(request)
            else:
                deferred = request.notifyFinish().addCallback(lambda _: request)
                deferred.request = request
                return deferred
        else:
            raise ValueError('Unexpected return value: %r' % (result,))
