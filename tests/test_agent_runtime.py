import asyncio
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agent_service import build_agent
from app.config import Settings
from app.schemas import AgentChatRequest


def test_real_langchain_agent_compiles_with_sqlite_checkpointer(tmp_path) -> None:
    """Актуальные зависимости собирают граф без обращения к внешнему API."""

    async def compile_agent() -> None:
        settings = Settings(
            proxyapi_key="test-key",
            agent_memory_path=tmp_path / "agent_memory.db",
        )
        request = AgentChatRequest(
            client_id="test-client",
            thread_id="test-thread",
            message="У автомобиля появился стук",
        )
        async with AsyncSqliteSaver.from_conn_string(
            str(settings.agent_memory_path)
        ) as checkpointer:
            await checkpointer.setup()
            agent = build_agent(request, settings, checkpointer)
            assert agent is not None

    asyncio.run(compile_agent())
    assert (tmp_path / "agent_memory.db").exists()


def test_sqlite_checkpointer_restores_state_and_isolates_threads(tmp_path) -> None:
    """Checkpoint переживает новое подключение и не смешивает диалоги."""

    class MemoryState(TypedDict):
        fact: str

    builder = StateGraph(MemoryState)
    builder.add_node("remember", lambda state: state)
    builder.add_edge(START, "remember")
    builder.add_edge("remember", END)
    database_path = tmp_path / "checkpoints.db"
    first_config = {"configurable": {"thread_id": "first-thread"}}
    second_config = {"configurable": {"thread_id": "second-thread"}}

    async def save_and_restore() -> None:
        async with AsyncSqliteSaver.from_conn_string(
            str(database_path)
        ) as first_checkpointer:
            first_graph = builder.compile(checkpointer=first_checkpointer)
            result = await first_graph.ainvoke(
                {"fact": "Lada Vesta"},
                config=first_config,
            )
            assert result["fact"] == "Lada Vesta"

        async with AsyncSqliteSaver.from_conn_string(
            str(database_path)
        ) as second_checkpointer:
            restored_graph = builder.compile(checkpointer=second_checkpointer)
            restored = await restored_graph.aget_state(first_config)
            isolated = await restored_graph.aget_state(second_config)
            assert restored.values["fact"] == "Lada Vesta"
            assert isolated.values == {}

    asyncio.run(save_and_restore())
