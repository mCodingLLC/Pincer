# Copyright Pincer 2021-Present
# Full MIT License can be found in `LICENSE` at the project root.

from abc import ABC, abstractmethod
from asyncio import Event, wait_for as _wait_for, get_running_loop, TimeoutError
from collections import deque
from typing import Any, Callable, Union

from .types import CheckFunction


class Processable(ABC):

    @abstractmethod
    def process(self, event_name: str, *args):
        pass

    def matches_event(self, event_name: str, *args):
        if self.event_name != event_name:
            return False

        self.return_value = args

        if self.check:
            return self.check(*args)

        return True


def _lowest_value(*args):
    return min(
        [
            n for n in args if n
        ]
    )


class _DiscordEvent(Event, Processable):
    """
    Attributes
    ----------
    return_value : Optional[str]
        Used to store the arguments from ``can_be_set`` so they can be
        returned later.
    """

    def __init__(
        self,
        event_name: str,
        check: CheckFunction
    ):
        """
        Parameters
        ----------
        event_name : str
            The name of the event.
        check : Optional[Callable[[Any], bool]]
            ``can_be_set`` only returns true if this function returns true.
            Will be ignored if set to None.
        """
        self.event_name = event_name
        self.check = check
        self.return_value = None
        super().__init__()

    def process(self, event_name: str, *args) -> bool:
        """
        Parameters
        ----------
        event_name : str
            The name of the event.
        *args : Any
            Arguments to evaluate check with.

        Returns
        -------
        bool
            Whether the event can be set
        """
        if self.matches_event(event_name, *args):
            self.set()


class LoopEmptyError(Exception):
    "Raised when the _LoopMgr is empty and cannot accept new item"


class _LoopMgr(Processable):

    def __init__(self, event_name: str, check: CheckFunction) -> None:
        self.event_name = event_name
        self.check = check

        self.can_expand = True
        self.events = deque()
        self.wait = Event()

    def process(self, event_name: str, *args):
        if not self.can_expand:
            return

        if self.matches_event(event_name, *args):
            self.events.append(args)
            self.wait.set()

    async def get_next(self):
        if len(self.events) == 0:
            if not self.can_expand:
                raise LoopEmptyError()

            self.wait.clear()
            await self.wait.wait()
            return self.events.popleft()
        else:
            return self.events.popleft()


class EventMgr:
    """
    Attributes
    ----------
    event_list : List[_DiscordEvent]
        The List of events that need to be processed.
    """

    def __init__(self):
        self.event_list: Processable = []

    def add_event(self, event_name: str, check: CheckFunction):
        """
        Parameters
        ----------
        event_name : str
            The type of event to listen for. Uses the same naming scheme as
            @Client.event.
        check : Optional[Callable[[Any], bool]]
            Expression to evaluate when checking if an event is valid. Will
            return be set if this event is true. Will be ignored if set to
            None.

        Returns
        -------
        _DiscordEvent
            Event that was added to the stack.
        """
        event = _DiscordEvent(
            event_name=event_name,
            check=check
        )
        self.event_list.append(event)
        return event

    def process_events(self, event_name, *args):
        """
        Parameters
        ----------
        event_name : str
            The name of the event to be processed.
        *args : Any
            The arguments returned from the middleware for this event.
        """
        for event in self.event_list:
            event.process(event_name, *args)

    async def wait_for(
        self,
        event_name: str,
        check: CheckFunction,
        timeout: Union[float, None]
    ) -> Any:
        """
        Parameters
        ----------
        event_name : str
            The type of event. It should start with `on_`. This is the same
            name that is used for @Client.event.
        check : Union[Callable[[Any], bool], None]
            This function only returns a value if this return true.
        timeout: Union[float, None]
            Amount of seconds before timeout. Use None for no timeout.

        Returns
        ------
        Any
            What the Discord API returns for this event.
        """
        event = self.add_event(event_name, check)
        try:
            await _wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            raise TimeoutError(
                "wait_for() timed out while waiting for an event."
            )
        self.event_list.remove(event)
        return event.return_value

    async def loop_for(
        self,
        event_name: str,
        check: Union[Callable[[Any], bool], None],
        iteration_timeout: Union[float, None],
        loop_timeout: Union[float, None],
    ) -> Any:
        """
        Parameters
        ----------
        event_name : str
            The type of event. It should start with `on_`. This is the same
            name that is used for @Client.event.
        check : Callable[[Any], bool]
            This function only returns a value if this return true.
        iteration_timeout: Union[float, None]
            Amount of seconds before timeout. Timeouts are for each loop.
        loop_timeout: Union[float, None]
            Amount of seconds before the entire loop times out. The generator
            will only raise a timeout error while it is waiting for an event.

        Yields
        ------
        Any
            What the Discord API returns for this event.
        """

        loop_mgr = _LoopMgr(event_name, check)
        self.event_list.append(loop_mgr)

        loop = get_running_loop()

        while True:
            start_time = loop.time()

            try:
                yield await _wait_for(
                    loop_mgr.get_next(),
                    timeout=_lowest_value(
                        loop_timeout, iteration_timeout
                    )
                )

            except TimeoutError:
                # Loop timed out. Run out the rest of the loop.
                loop_mgr.can_expand = False
                try:
                    while True:
                        yield await loop_mgr.get_next()
                except LoopEmptyError:
                    raise TimeoutError(
                        "loop_for() timed out while waiting for an event"
                    )

            loop_timeout -= loop.time() - start_time

            # loop_timeout can be below 0 if the user's code in the for loop
            # takes longer than the time left in loop_timeout
            if loop_timeout <= 0:
                break
