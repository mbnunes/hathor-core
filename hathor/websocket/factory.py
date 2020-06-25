import json
from collections import defaultdict, deque
from typing import Any, DefaultDict, Deque, Dict, Optional, Set

from autobahn.twisted.websocket import WebSocketServerFactory
from twisted.internet import reactor

from hathor.conf import HathorSettings
from hathor.indexes import WalletIndex
from hathor.metrics import Metrics
from hathor.p2p.rate_limiter import RateLimiter
from hathor.pubsub import HathorEvents
from hathor.websocket.protocol import HathorAdminWebsocketProtocol

settings = HathorSettings()

# CONTROLLED_TYPES define each Rate Limit parameter for each message type that should be limited
# buffer_size (int): size of the deque that will hold the messages that will be processed in the future
# time_buffering (int): Interval that we will try to process the deque with unprocessed messages
# max_hits (int) and hits_window_seconds (int): together they define the Rate Limit
# It's how many hits can this message make in the window interval

CONTROLLED_TYPES: Dict[str, Dict[str, Any]] = {
    HathorEvents.NETWORK_NEW_TX_ACCEPTED.value: {
        'buffer_size': 20,
        'time_buffering': 0.1,
        'max_hits': 20,
        'hits_window_seconds': 2,
    },
    HathorEvents.WALLET_OUTPUT_RECEIVED.value: {
        'buffer_size': 20,
        'time_buffering': 0.1,
        'max_hits': 10,
        'hits_window_seconds': 2,
    },
    HathorEvents.WALLET_INPUT_SPENT.value: {
        'buffer_size': 20,
        'time_buffering': 0.1,
        'max_hits': 10,
        'hits_window_seconds': 2,
    },
    HathorEvents.WALLET_BALANCE_UPDATED.value: {
        'buffer_size': 3,
        'time_buffering': 0.4,
        'max_hits': 3,
        'hits_window_seconds': 1,
    }
}

# these events should only be sent to websockets subscribed to a specific address, not broadcast
ADDRESS_EVENTS = [
    HathorEvents.WALLET_ADDRESS_HISTORY.value,
    HathorEvents.WALLET_ELEMENT_WINNER.value,
    HathorEvents.WALLET_ELEMENT_VOIDED.value
]

# Possible channels that can be subscribed on the websocket
# Each channel has a list of events that emits.
CHANNELS = {
    'wallet-service': [
        HathorEvents.NETWORK_NEW_TX_ACCEPTED.value,
    ]
}


class HathorAdminWebsocketFactory(WebSocketServerFactory):
    """ Factory of the admin websocket protocol so we can subscribe to events and
        send messages in the Admin page to clients when the events are published
    """
    protocol = HathorAdminWebsocketProtocol

    def buildProtocol(self, addr):
        return self.protocol(self)

    def __init__(self, metrics: Optional[Metrics] = None, wallet_index: Optional[WalletIndex] = None):
        """
        :param metrics: If not given, a new one is created.
        :type metrics: :py:class:`hathor.metrics.Metrics`
        """
        # Opened websocket connections so I can broadcast messages later
        self.connections: Set[HathorAdminWebsocketProtocol] = set()

        # Connections that are subscribed for each channel
        self.subscribed_connections: DefaultDict[str, Set[HathorAdminWebsocketProtocol]] = defaultdict(set)

        # Websocket connection for each address
        self.address_connections: DefaultDict[str, Set[HathorAdminWebsocketProtocol]] = defaultdict(set)
        super().__init__()

        # Limit the send message rate for specific type of data
        self.rate_limiter = RateLimiter(reactor=reactor)
        # Stores the buffer of messages that exceeded the rate limit and will be sent
        self.buffer_deques: Dict[str, Deque[Dict[str, Any]]] = {}

        self.metrics = metrics
        self.wallet_index = wallet_index

        self.is_running = False

    def start(self):
        self.is_running = True

        # Start limiter
        self._setup_rate_limit()

        # Start metric sender
        self._schedule_and_send_metric()

    def stop(self):
        self.is_running = False

    def _setup_rate_limit(self):
        """ Set the limit of the RateLimiter and start the buffer deques with BUFFER_SIZE
        """
        for control_type, config in CONTROLLED_TYPES.items():
            self.rate_limiter.set_limit(control_type, config['max_hits'], config['hits_window_seconds'])
            self.buffer_deques[control_type] = deque(maxlen=config['buffer_size'])

    def _schedule_and_send_metric(self):
        """ Send dashboard metric to websocket and schedule next message
        """
        data = {
            'transactions': self.metrics.transactions,
            'blocks': self.metrics.blocks,
            'best_block_height': self.metrics.best_block_height,
            'hash_rate': self.metrics.hash_rate,
            'block_hash_rate': self.metrics.block_hash_rate,
            'tx_hash_rate': self.metrics.tx_hash_rate,
            'network_hash_rate': self.metrics.tx_hash_rate + self.metrics.block_hash_rate,
            'peers': self.metrics.peers,
            'type': 'dashboard:metrics',
            'time': reactor.seconds(),
        }
        self.broadcast_message(data)

        if self.is_running:
            # Schedule next message
            reactor.callLater(1, self._schedule_and_send_metric)

    def subscribe(self, pubsub):
        """ Subscribe to defined events for the pubsub received
        """
        events = [
            HathorEvents.NETWORK_NEW_TX_ACCEPTED,
            HathorEvents.WALLET_OUTPUT_RECEIVED,
            HathorEvents.WALLET_INPUT_SPENT,
            HathorEvents.WALLET_BALANCE_UPDATED,
            HathorEvents.WALLET_KEYS_GENERATED,
            HathorEvents.WALLET_GAP_LIMIT,
            HathorEvents.WALLET_HISTORY_UPDATED,
            HathorEvents.WALLET_ADDRESS_HISTORY,
            HathorEvents.WALLET_ELEMENT_WINNER,
            HathorEvents.WALLET_ELEMENT_VOIDED,
        ]

        for event in events:
            pubsub.subscribe(event, self.handle_publish)

    def handle_publish(self, key, args):
        """ This method is called when pubsub publishes an event that we subscribed
            Then we broadcast the data to all connected clients
        """
        data = self.serialize_message_data(key, args)
        data['type'] = key.value
        self.send_or_enqueue(data)

    def serialize_message_data(self, event, args):
        """ Receives the event and the args from the pubsub
            and serializes the data so it can be passed in the websocket
        """
        # Ready events don't need extra serialization
        ready_events = [
            HathorEvents.WALLET_KEYS_GENERATED,
            HathorEvents.WALLET_GAP_LIMIT,
            HathorEvents.WALLET_HISTORY_UPDATED,
            HathorEvents.WALLET_ADDRESS_HISTORY,
        ]
        data = args.__dict__
        if event in ready_events:
            return data
        elif event == HathorEvents.WALLET_OUTPUT_RECEIVED:
            data['output'] = data['output'].to_dict()
            return data
        elif event == HathorEvents.WALLET_INPUT_SPENT:
            data['output_spent'] = data['output_spent'].to_dict()
            return data
        elif event == HathorEvents.NETWORK_NEW_TX_ACCEPTED:
            tx = data['tx']
            data = tx.to_json_extended()
            data['is_block'] = tx.is_block
            return data
        elif event == HathorEvents.WALLET_BALANCE_UPDATED:
            data['balance'] = data['balance'][settings.HATHOR_TOKEN_UID]._asdict()
            return data
        else:
            raise ValueError('Should never have entered here! We dont know this event')

    def execute_send(self, data: Dict[str, Any], connections: Set[HathorAdminWebsocketProtocol]) -> None:
        """ Send data in ws message for the connections
        """
        payload = json.dumps(data).encode('utf-8')
        for c in connections:
            c.sendMessage(payload, False)

    def broadcast_message(self, data: Dict[str, Any]) -> None:
        """ Broadcast the update message to the connections
        """
        self.execute_send(data, self.connections)

    def send_message(self, data: Dict[str, Any]) -> None:
        """ Check if should broadcast the message to all connections or send directly to some connections only
        """
        if data['type'] in ADDRESS_EVENTS:
            # This ws message will only be sent if the address was subscribed
            if data['address'] in self.address_connections:
                self.execute_send(data, self.address_connections[data['address']])
        else:
            self.broadcast_message(data)

    def send_or_enqueue(self, data):
        """ Try to broadcast the message, or enqueue it when rate limit is exceeded and we've been throttled.
            Enqueued messages are automatically sent after a while if they are not discarded first.
            A message is discarded when new messages arrive and the queue buffer is full.
            Rate limits change according to the message type, which is obtained from data['type'].

            :param data: message to be sent
            :type data: Dict[string, X] -> X can be different types, depending on the type of message
        """
        if data['type'] in CONTROLLED_TYPES:
            # This type is controlled, so I need to check the deque
            if len(self.buffer_deques[data['type']]) or not self.rate_limiter.add_hit(data['type']):
                # If I am already with a buffer or if I hit the limit now, I enqueue for later
                self.enqueue_for_later(data)
            else:
                data['throttled'] = False
                self.send_message(data)
        else:
            self.send_message(data)

        # Check if the data is supposed to be sent in one of the subscribed channels
        # Even though it's better for the code to have a dict with the event type as the key
        # and the channels as the value (EVENTS[event] would be an array of channels to send)
        # it seems more logical to think which events each channel needs
        # So, since the dict and the arrays are not going to be big, I think it's better
        # to keep more logical to fill the dict in case we add a new channel
        for key, value in CHANNELS.items():
            if data['type'] in value:
                self.send_message_channel(data, key)

    def send_message_channel(self, data: Dict[str, Any], channel: str) -> None:
        """ Send the data for all the connections of the channel
        """
        self.execute_send(data, self.subscribed_connections[channel])

    def enqueue_for_later(self, data):
        """ Add this date to the correct deque to be processed later
            If this deque is not programed to be called later yet, we call it

            :param data: message to be sent
            :type data: Dict[string, X] -> X can be different types, depending on the type of message
        """
        # Add data to deque
        # We always add the new messages in the end
        # Adding parameter deque=True, so the admin can know this message was delayed
        data['throttled'] = True
        self.buffer_deques[data['type']].append(data)
        if len(self.buffer_deques[data['type']]) == 1:
            # If it's the first time we hit the limit (only one message in deque), we schedule process_deque
            reactor.callLater(CONTROLLED_TYPES[data['type']]['time_buffering'], self.process_deque,
                              data_type=data['type'])

    def process_deque(self, data_type):
        """ Process the deque and check if I have limit to send the messages now

            :param data_type: Type of the message to be sent
            :type data_type: string
        """
        while len(self.buffer_deques[data_type]) > 0:
            if self.rate_limiter.add_hit(data_type):
                # We always process the older message first
                data = self.buffer_deques[data_type].popleft()
                if len(self.buffer_deques[data_type]) == 0:
                    data['throttled'] = False
                self.send_message(data)
            else:
                reactor.callLater(CONTROLLED_TYPES[data_type]['time_buffering'], self.process_deque,
                                  data_type=data_type)
                break

    def handle_message(self, connection: HathorAdminWebsocketProtocol, data: bytes) -> None:
        """ General message handler, detects type and deletages to specific handler."""
        message = json.loads(data)
        # we only handle ping messages for now
        if message['type'] == 'ping':
            self._handle_ping(connection, message)
        elif message['type'] == 'subscribe_address':
            self._handle_subscribe_address(connection, message)
        elif message['type'] == 'unsubscribe_address':
            self._handle_unsubscribe_address(connection, message)
        elif message['type'] == 'subscribe':
            self._handle_subscribe_channel(connection, message)

    def _handle_ping(self, connection: HathorAdminWebsocketProtocol, message: Dict[Any, Any]) -> None:
        """ Handler for ping message, should respond with a simple {"type": "pong"}"""
        payload = json.dumps({'type': 'pong'}).encode('utf-8')
        connection.sendMessage(payload, False)

    def _handle_subscribe_address(self, connection: HathorAdminWebsocketProtocol, message: Dict[Any, Any]) -> None:
        """ Handler for subscription to an address, consideirs subscription limits."""
        addr: str = message['address']
        subs: Set[str] = connection.subscribed_to
        if len(subs) >= settings.WS_MAX_SUBS_ADDRS_CONN:
            payload = json.dumps({'message': 'Reached maximum number of subscribed '
                                             f'addresses ({settings.WS_MAX_SUBS_ADDRS_CONN}).',
                                  'type': 'subscribe_address', 'success': False}).encode('utf-8')
        elif self.wallet_index and _count_empty(subs, self.wallet_index) >= settings.WS_MAX_SUBS_ADDRS_EMPTY:
            payload = json.dumps({'message': 'Reached maximum number of subscribed '
                                             f'addresses without output ({settings.WS_MAX_SUBS_ADDRS_EMPTY}).',
                                  'type': 'subscribe_address', 'success': False}).encode('utf-8')
        else:
            self.address_connections[addr].add(connection)
            connection.subscribed_to.add(addr)
            payload = json.dumps({'type': 'subscribe_address', 'success': True}).encode('utf-8')
        connection.sendMessage(payload, False)

    def _handle_unsubscribe_address(self, connection: HathorAdminWebsocketProtocol, message: Dict[Any, Any]) -> None:
        """ Handler for unsubscribing from an address, also removes address connection set if it ends up empty."""
        addr = message['address']
        if addr in self.address_connections and connection in self.address_connections[addr]:
            connection.subscribed_to.remove(addr)
            self._remove_connection_from_address_dict(connection, addr)
            # Reply back to the client
            payload = json.dumps({'type': 'unsubscribe_address', 'success': True}).encode('utf-8')
            connection.sendMessage(payload, False)

    def _remove_connection_from_address_dict(self, connection: HathorAdminWebsocketProtocol, address: str) -> None:
        """ Remove a connection from the address connections dict
            If this was the last connection for this address, we remove the key
        """
        self.address_connections[address].remove(connection)
        # If this was the last connection for this address, we delete it from the dict
        if len(self.address_connections[address]) == 0:
            del self.address_connections[address]

    def _handle_subscribe_channel(self, connection: HathorAdminWebsocketProtocol, message: Dict[Any, Any]) -> None:
        """ Handler for subscription to a channel."""
        channel: str = message['channel']
        if channel in CHANNELS:
            # Remove from the all connections set
            self.connections.remove(connection)
            # Add the connection to the set
            self.subscribed_connections[channel].add(connection)
        # Reply back as subscribed success
        payload = json.dumps({'type': 'subscribed', 'channel': channel, 'success': True}).encode('utf-8')
        connection.sendMessage(payload, False)

    def connection_closed(self, connection: HathorAdminWebsocketProtocol) -> None:
        """ Called when a ws connection is closed
            We should remove it from self.connections and from self.address_connections set for each address
        """
        if connection in self.connections:
            # With connections subscribing to an specific channel, we need to do this check
            # In the future all connections should subscribe to channels
            self.connections.remove(connection)

        for address in connection.subscribed_to:
            self._remove_connection_from_address_dict(connection, address)

        # Removing from subsbribed channels set
        # Should we have an easier way to check in each channels the connections is subscribed to?
        # For now we have only one channel, so this shouldn't be a problem
        for conn_set in self.subscribed_connections.values():
            if connection in conn_set:
                conn_set.remove(connection)


def _count_empty(addresses: Set[str], wallet_index: WalletIndex) -> int:
    """ Count how many of the addresses given are empty (have no outputs)."""
    return sum(1 for addr in addresses if not wallet_index.get_from_address(addr))
