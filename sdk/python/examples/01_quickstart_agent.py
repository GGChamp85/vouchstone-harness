"""Quickstart: a working agent with 5-layer memory, fully offline.

Runs with zero configuration and zero network access:

    pip install vouchstone-sdk
    python examples/01_quickstart_agent.py

`local_only=True` keeps every memory layer in-process (documented behavior --
no Redis/ChromaDB/Neo4j needed for a first run; point the corresponding URLs
at real backends when you have them and nothing else changes).
"""
import asyncio

from vouchstone_sdk import Agent, AgentConfig
from vouchstone_sdk.types import AgentResponse, MemoryContext, Message


class EchoAgent(Agent):
    """The smallest possible agent: implement run(), the SDK does the rest.

    A real agent would call an LLM here (see the llm-openai / llm-anthropic
    extras); the memory pipeline around run() is identical either way.
    """

    async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
        turns_in_working_memory = len(context.working_memory)
        return AgentResponse(
            content=(
                f"You said: {message.content!r}. "
                f"Working memory holds {turns_in_working_memory} prior entr(y/ies) this session."
            ),
            usage={"tokens_in": len(message.content) // 4, "tokens_out": 24},
        )


async def main() -> None:
    agent = EchoAgent(AgentConfig(name="quickstart-agent"))
    await agent.initialize(agent_id="quickstart-agent", local_only=True)
    agent.start_session()

    for text in ("hello", "do you remember me?"):
        response = await agent.process(Message(role="user", content=text))
        print(f"> {text}\n{response.content}\n")

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
