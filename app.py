"""Inventory Q&A agent: chat with an Excel dataset using Groq, pandas code execution and web search."""

from pathlib import Path

import pandas as pd
import streamlit as st
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import StructuredTool, Tool
from langchain_experimental.tools.python.tool import PythonAstREPLTool
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field

# LangChain 1.x moved the legacy AgentExecutor API into `langchain_classic`.
try:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
except ImportError:
    from langchain_classic.agents import AgentExecutor, create_tool_calling_agent

DATA_FILE = Path(__file__).parent / "Inventory-Records-Sample-Data.xlsx"
MODEL_NAME = "openai/gpt-oss-120b"

SYSTEM_PROMPT = (
    "You are an expert inventory analysis assistant. When answering questions about "
    "stock numbers, costs, units, or table data, you MUST write and execute Python code "
    "using the Data Query tool against the 'df' DataFrame. When answering general "
    "definitions or external supply chain terms, use the Search tool. Always synthesize "
    "your final findings into clear, plain English for the user."
)

st.set_page_config(page_title="Inventory Analysis Agent", layout="centered")


@st.cache_data(show_spinner=False)
def load_data(path: str) -> pd.DataFrame:
    """Load and clean the inventory sheet (headers are on row 6)."""
    df = pd.read_excel(path, skiprows=5)
    df = df.dropna(how="all", axis=1).dropna(how="all", axis=0)
    # Replace line breaks with a single space and collapse any doubled spaces.
    df.columns = [" ".join(str(c).replace("\n", " ").split()) for c in df.columns]
    return df.reset_index(drop=True)


class DataQueryInput(BaseModel):
    code: str = Field(
        description="Python/Pandas code to run against the DataFrame `df`. Use print() to show results."
    )


def build_executor(df: pd.DataFrame, api_key: str) -> AgentExecutor:
    llm = ChatGroq(model=MODEL_NAME, temperature=0, api_key=api_key, timeout=120, max_retries=1)

    repl = PythonAstREPLTool(locals={"df": df, "pd": pd})
    # Wrap the REPL so the tool has one clearly named `code` argument; some models
    # (e.g. gpt-oss on Groq) otherwise omit the REPL's generic `query` field.
    python_tool = StructuredTool.from_function(
        func=lambda code: repl.run(code),
        name="Data_Query",
        args_schema=DataQueryInput,
        description=(
            "Executes Python/Pandas code against the inventory DataFrame `df` to calculate, "
            "aggregate, filter, sort, sum, or inspect inventory figures (stock, purchases, "
            "units sold, cost prices). Pass the code in the `code` argument and use print() "
            "to output results. Columns: " + ", ".join(map(str, df.columns))
        ),
    )
    search_tool = Tool(
        name="Search",
        func=DuckDuckGoSearchRun().run,
        description=(
            "Searches the web to look up business terminology, inventory formulas, "
            "definitions, and external supply chain context. Input is a search query string."
        ),
    )
    tools = [python_tool, search_tool]

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT + "\n\nThe DataFrame `df` has these columns: {columns}"),
            MessagesPlaceholder("chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ]
    ).partial(columns=", ".join(map(str, df.columns)).replace("{", "(").replace("}", ")"))

    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(
        agent=agent, tools=tools, verbose=True, handle_parsing_errors=True, max_iterations=8
    )


def history_for_agent(messages: list[dict]) -> list[tuple[str, str]]:
    """Convert earlier chat turns (excluding the newest user message) to prompt tuples."""
    return [("human" if m["role"] == "user" else "ai", m["content"]) for m in messages[:-1]]


AVATARS = {"user": ":material/person:", "assistant": ":material/computer:"}


def main() -> None:
    st.title("Inventory Analysis Agent")
    st.write("Ask questions about the inventory data or supply chain terms.")

    st.sidebar.header("Settings")
    api_key = st.sidebar.text_input("Enter Groq API Key", type="password")
    st.sidebar.caption(f"Model: {MODEL_NAME}")

    try:
        df = load_data(str(DATA_FILE))
    except FileNotFoundError:
        st.error(f"Dataset not found: {DATA_FILE}")
        st.stop()
    except Exception as exc:
        st.error(f"Failed to load the dataset: {exc}")
        st.stop()

    with st.expander("Preview Inventory Dataset"):
        st.dataframe(df.head(10))

    if not api_key:
        st.info("Please enter your Groq API key in the sidebar to start chatting.")
        st.stop()

    try:
        # Rebuild only when the key changes.
        if st.session_state.get("executor_key") != api_key:
            st.session_state.executor = build_executor(df, api_key)
            st.session_state.executor_key = api_key
    except Exception as exc:
        st.error(f"Could not initialise the agent: {exc}")
        st.stop()

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"], avatar=AVATARS[msg["role"]]):
            st.markdown(msg["content"])

    question = st.chat_input("Ask a question about the inventory data or supply chain terms...")
    if not question:
        return

    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=AVATARS["user"]):
        st.markdown(question)

    with st.chat_message("assistant", avatar=AVATARS["assistant"]):
        with st.spinner("Analyzing..."):
            try:
                result = st.session_state.executor.invoke(
                    {
                        "input": question,
                        "chat_history": history_for_agent(st.session_state.messages),
                    }
                )
                answer = str(result.get("output", "")) or "I couldn't produce an answer."
            except Exception as exc:
                answer = f"Sorry, something went wrong while analysing your question: {exc}"
        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})


main()
