import typing_extensions as typing
from fntypes.result import Error, Ok, Result

from telegrinder.api.api import API
from telegrinder.bot.dispatch.context import Context
from telegrinder.msgspec_utils import repr_type
from telegrinder.node.composer import NodeSession, compose_nodes
from telegrinder.tools.adapter.abc import ABCAdapter, Event
from telegrinder.tools.adapter.errors import AdapterError
from telegrinder.types.objects import Update

if typing.TYPE_CHECKING:
    from telegrinder.node.base import Node


class NodeAdapter[*Nodes](ABCAdapter[Update, Event[tuple[*Nodes]]]):
    def __init__(self, *nodes: *Nodes) -> None:
        self.nodes = nodes

    def __repr__(self) -> str:
        return "<{}: adapt Update -> ({})>".format(
            self.__class__.__name__,
            ", ".join(repr_type(node) for node in self.nodes),
        )

    async def adapt(
        self,
        api: API,
        update: Update,
        context: Context,
    ) -> Result[Event[tuple[*Nodes]], AdapterError]:
        result = await compose_nodes(
            nodes={str(i): typing.cast(type["Node"], node) for i, node in enumerate(self.nodes)},
            ctx=context,
            data={Update: update, API: api},
        )

        match result:
            case Ok(collection):
                sessions: list[NodeSession] = list(collection.sessions.values())
                return Ok(Event(tuple(sessions)))  # type: ignore
            case Error(err):
                return Error(AdapterError(f"Couldn't compose nodes, error: {err}."))


__all__ = ("NodeAdapter",)
