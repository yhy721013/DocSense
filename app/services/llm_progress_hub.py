from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, DefaultDict, Dict, List, Tuple


Subscriber = Callable[[Dict[str, Any]], None]


class LLMProgressHub:
    def __init__(self) -> None:
        self._subscribers: DefaultDict[Tuple[str, str], List[Subscriber]] = defaultdict(list)
        self._latest: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def subscribe(self, business_type: str, business_key: str, callback: Subscriber) -> None:
        key = (business_type, business_key)
        self._subscribers[key].append(callback)
        latest = self._latest.get(key)
        if latest is not None:
            callback(latest)

    def unsubscribe(self, business_type: str, business_key: str, callback: Subscriber) -> None:
        key = (business_type, business_key)
        listeners = self._subscribers.get(key, [])
        self._subscribers[key] = [listener for listener in listeners if listener is not callback]
        if not self._subscribers[key]:
            self._subscribers.pop(key, None)

    def get_latest(self, business_type: str, business_key: str) -> Dict[str, Any] | None:
        return self._latest.get((business_type, business_key))

    def publish(self, business_type: str, business_key: str, payload: Dict[str, Any]) -> None:
        key = (business_type, business_key)
        self._latest[key] = payload
        for callback in list(self._subscribers.get(key, [])):
            callback(payload)
