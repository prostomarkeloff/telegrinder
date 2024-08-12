import asyncio
import dataclasses
import typing
from contextlib import suppress

from fntypes.error import UnwrapError

from telegrinder.bot.cute_types.update import UpdateCute
from telegrinder.bot.dispatch.context import Context
from telegrinder.node.base import (
    BaseNode,
    ComposeError,
    Node,
    NodeScope,
    collect_context_annotations,
    collect_nodes,
)
from telegrinder.tools.magic import magic_bundle

CONTEXT_STORE_NODES_KEY = "node_ctx"


async def run_node_composers(
    tasks: typing.Iterable[asyncio.Task["NodeSession"]],
) -> bool:
    with suppress(ComposeError, UnwrapError):
        await asyncio.gather(*tasks)
        return True
    return False


async def compose_node(
    _node: type[Node],
    update: UpdateCute,
    ctx: Context,
) -> "NodeSession":
    node = _node.as_node()
    context = NodeCollection({})
    node_ctx: dict[type[Node], "NodeSession"] = ctx.get_or_set(CONTEXT_STORE_NODES_KEY, {})

    for name, subnode in node.get_sub_nodes().items():
        if subnode in node_ctx:
            context.sessions[name] = node_ctx[subnode]
        else:
            context.sessions[name] = await compose_node(subnode, update, ctx)

            if getattr(subnode, "scope", None) is NodeScope.PER_EVENT:
                node_ctx[subnode] = context.sessions[name]

    for name, annotation in node.get_context_annotations().items():
        context.sessions[name] = NodeSession(
            None, await node.compose_context_annotation(annotation, update, ctx), {}
        )

    if node.is_generator():
        generator = typing.cast(typing.AsyncGenerator[typing.Any, None], node.compose(**context.values()))
        value = await generator.asend(None)
    else:
        generator = None
        value = await node.compose(**context.values())  # type: ignore

    return NodeSession(_node, value, context.sessions, generator)


async def compose_nodes(
    update: UpdateCute,
    ctx: Context,
    nodes: dict[str, type[Node]],
    node_class: type[Node] | None = None,
    context_annotations: dict[str, typing.Any] | None = None,
) -> "NodeCollection | None":
    node_sessions: dict[str, NodeSession] = {}
    node_ctx: dict[type[Node], "NodeSession"] = ctx.get_or_set(CONTEXT_STORE_NODES_KEY, {})
    node_tasks: dict[str, asyncio.Task[NodeSession]] = {}

    for name, node_t in nodes.items():
        scope = getattr(node_t, "scope", None)

        if scope is NodeScope.PER_EVENT and node_t in node_ctx:
            node_sessions[name] = node_ctx[node_t]
            continue
        elif scope is NodeScope.GLOBAL and hasattr(node_t, "_value"):
            node_sessions[name] = getattr(node_t, "_value")
            continue

        node_tasks[name] = asyncio.Task(compose_node(node_t, update, ctx))

    if not await run_node_composers(node_tasks.values()):
        await NodeCollection(node_sessions).close_all()
        return None

    for name, task in node_tasks.items():
        node_sessions[name] = session = task.result()
        assert session.node_type is not None
        node_t = session.node_type
        scope = getattr(node_t, "scope", None)

        if scope is NodeScope.PER_EVENT:
            node_ctx[session.node_type] = session
        elif scope is NodeScope.GLOBAL:
            setattr(node_t, "_value", session)

    if context_annotations:
        node_class = node_class or BaseNode
        _node_tasks: dict[str, asyncio.Task[NodeSession]] = {
            name: asyncio.Task(node_class.compose_context_annotation(annotation, update, ctx))
            for name, annotation in context_annotations.items()
        }

        if not await run_node_composers(_node_tasks.values()):
            await NodeCollection(node_sessions).close_all()
            return None

        for name, task in _node_tasks.items():
            node_sessions[name] = task.result()

    return NodeCollection(node_sessions)


@dataclasses.dataclass(slots=True, repr=False)
class NodeSession:
    node_type: type[Node] | None
    value: typing.Any
    subnodes: dict[str, typing.Self]
    generator: typing.AsyncGenerator[typing.Any, None] | None = None

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.value}" + ("ACTIVE>" if self.generator else ">")

    async def close(
        self,
        with_value: typing.Any | None = None,
        scopes: tuple[NodeScope, ...] = (NodeScope.PER_CALL,),
    ) -> None:
        if self.node_type and getattr(self.node_type, "scope", None) not in scopes:
            return

        for subnode in self.subnodes.values():
            await subnode.close(scopes=scopes)

        if self.generator is None:
            return
        try:
            await self.generator.asend(with_value)
        except StopAsyncIteration:
            self.generator = None


class NodeCollection:
    __slots__ = ("sessions",)

    def __init__(self, sessions: dict[str, NodeSession]) -> None:
        self.sessions = sessions

    def __repr__(self) -> str:
        return "<{}: sessions={!r}>".format(self.__class__.__name__, self.sessions)

    def values(self) -> dict[str, typing.Any]:
        return {name: session.value for name, session in self.sessions.items()}

    async def close_all(
        self,
        with_value: typing.Any | None = None,
        scopes: tuple[NodeScope, ...] = (NodeScope.PER_CALL,),
    ) -> None:
        for session in self.sessions.values():
            await session.close(with_value, scopes=scopes)


@dataclasses.dataclass(slots=True, repr=False)
class Composition:
    func: typing.Callable[..., typing.Any]
    is_blocking: bool
    node_class: type[Node] = dataclasses.field(default_factory=lambda: BaseNode)
    nodes: dict[str, type[Node]] = dataclasses.field(init=False)
    context_annotations: dict[str, typing.Any] = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.nodes = collect_nodes(self.func)
        self.context_annotations = collect_context_annotations(self.func)

    def __repr__(self) -> str:
        return "<{}: for function={!r} with nodes={!r}, context_annotations={!r}>".format(
            ("blocking " if self.is_blocking else "") + self.__class__.__name__,
            self.func.__qualname__,
            self.nodes,
            self.context_annotations,
        )

    async def compose_nodes(self, update: UpdateCute, context: Context) -> NodeCollection | None:
        return await compose_nodes(
            update=update,
            ctx=context,
            nodes=self.nodes,
            node_class=self.node_class,
            context_annotations=self.context_annotations,
        )

    async def __call__(self, **kwargs: typing.Any) -> typing.Any:
        return await self.func(**magic_bundle(self.func, kwargs, start_idx=0, bundle_ctx=False))  # type: ignore


__all__ = ("NodeCollection", "NodeSession", "Composition", "compose_node", "compose_nodes")
