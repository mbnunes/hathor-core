from collections import defaultdict
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, List

from hathor.util import ReactorThread

if TYPE_CHECKING:
    from twisted.internet.interfaces import IReactorCore  # noqa: F401


class HathorEvents(Enum):
    """
        NETWORK_NEW_ACCEPTED:
            Triggered when a new tx/block is accepted in the network
            Publishes a tx/block object

        NETWORK_PEER_CONNECTED:
            Triggered when a new peer connects to the network
            Publishes the peer protocol

        NETWORK_PEER_DISCONNECTED:
            Triggered when a peer disconnects from the network
            Publishes the peer protocol

        STORAGE_TX_VOIDED:
            Triggered when a tx is marked as voided because of a conflict
            Publishes the tx object

        STORAGE_TX_WINNER:
            Triggered when a tx is marked as winner of a conflict
            Publishes the tx object

        WALLET_OUTPUT_RECEIVED:
            Triggered when a wallet receives a new output
            Publishes an UnspentTx object and the new total number of tx in the Wallet (total=int, output=UnspentTx)

        WALLET_INPUT_SPENT:
            Triggered when a wallet spends an output
            Publishes a SpentTx object (output_spent=SpentTx)

        WALLET_BALANCE_UPDATED:
            Triggered when the balance of the wallet changes
            Publishes a hathor.wallet.base_wallet.WalletBalance namedtuple (locked, available)

        WALLET_KEYS_GENERATED:
            Triggered when new keys are generated by the wallet and returns the quantity of keys generated
            Publishes an int (keys_count=int)

        WALLET_HISTORY_UPDATED:
            Triggered when the wallet history is updated by a voided/winner transaction

        WALLET_ADDRESS_HISTORY:
            Triggered when the we receive any transaction and send input/output by each address

        WALLET_ELEMENT_WINNER:
            Triggered when a wallet element is marked as winner

        WALLET_ELEMENT_VOIDED:
            Triggered when a wallet element is marked as voided
    """
    MANAGER_ON_START = 'manager:on_start'
    MANAGER_ON_STOP = 'manager:on_stop'

    NETWORK_PEER_CONNECTED = 'network:peer_connected'

    NETWORK_PEER_DISCONNECTED = 'network:peer_disconnected'

    NETWORK_NEW_TX_ACCEPTED = 'network:new_tx_accepted'

    STORAGE_TX_VOIDED = 'storage:tx_voided'

    STORAGE_TX_WINNER = 'storage:tx_winner'

    WALLET_OUTPUT_RECEIVED = 'wallet:output_received'

    WALLET_INPUT_SPENT = 'wallet:output_spent'

    WALLET_BALANCE_UPDATED = 'wallet:balance_updated'

    WALLET_KEYS_GENERATED = 'wallet:keys_generated'

    WALLET_GAP_LIMIT = 'wallet:gap_limit'

    WALLET_HISTORY_UPDATED = 'wallet:history_updated'

    WALLET_ADDRESS_HISTORY = 'wallet:address_history'

    WALLET_ELEMENT_WINNER = 'wallet:element_winner'

    WALLET_ELEMENT_VOIDED = 'wallet:element_voided'


class EventArguments:
    """Simple object for storing event arguments.
    """

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


PubSubCallable = Callable[[HathorEvents, EventArguments], None]


class PubSubManager:
    """Manages a pub/sub pattern bus.

    It is used to let independent objects respond to events.
    """

    _subscribers: Dict[HathorEvents, List[PubSubCallable]]

    def __init__(self, reactor: 'IReactorCore') -> None:
        self._subscribers = defaultdict(list)
        self.reactor = reactor

    def subscribe(self, key: HathorEvents, fn: PubSubCallable) -> None:
        """Subscribe to a specific event.

        :param key: Name of the key to which to subscribe.
        :type key: string

        :param fn: A function to be called when an event with `key` is published.
        :type fn: function
        """
        if fn not in self._subscribers[key]:
            self._subscribers[key].append(fn)

    def unsubscribe(self, key: HathorEvents, fn: PubSubCallable) -> None:
        """Unsubscribe from a specific event.
        """
        if fn in self._subscribers[key]:
            self._subscribers[key].remove(fn)

    def publish(self, key: HathorEvents, **kwargs: Any) -> None:
        """Publish a new event.

        :param key: Key of the new event.
        :type key: string

        :param **kwargs: Named arguments to be given to the functions that will be called with this event.
        :type **kwargs: dict
        """
        reactor_thread = ReactorThread.get_current_thread(self.reactor)

        args = EventArguments(**kwargs)
        for fn in self._subscribers[key]:
            if reactor_thread == ReactorThread.NOT_RUNNING:
                fn(key, args)
            elif reactor_thread == ReactorThread.MAIN_THREAD:
                self.reactor.callLater(0, fn, key, args)
            elif reactor_thread == ReactorThread.NOT_MAIN_THREAD:
                # We're taking a conservative approach, since not all functions might need to run
                # on the main thread [yan 2019-02-20]
                self.reactor.callFromThread(fn, key, args)
