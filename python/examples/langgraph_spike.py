"""Sample LangGraph agent showing cost/line attribution through a framework.

Each node calls traced_llm_call() from a different line. LangGraph's Pregel
runner sits between the invoke() call and these node functions on the call
stack — attribution still resolves to *this* file's lines, not langgraph's
internals, which is the whole point of traced_llm_call's stack walk.

Run: python -m examples.langgraph_spike
Set AIGHT_API_KEY first to also push the result to your AIght Workspace.
"""
import os
from typing import TypedDict

from aight.remote import push
from aight.tracing import COST_PROCESSOR, traced_llm_call
from langgraph.graph import END, StateGraph


class State(TypedDict):
    question: str
    plan: str
    answer: str


def plan_step(state: State) -> State:
    traced_llm_call("gpt-4o-mini", input_tokens=180, output_tokens=60)
    return {**state, "plan": f"plan for: {state['question']}"}


def retrieve_step(state: State) -> State:
    # Deliberately expensive: simulates re-embedding the full corpus.
    traced_llm_call("gpt-4o-mini", input_tokens=900, output_tokens=40)
    return state


def answer_step(state: State) -> State:
    traced_llm_call("gpt-4o", input_tokens=400, output_tokens=250)
    return {**state, "answer": f"answer using {state['plan']}"}


def build_graph():
    graph = StateGraph(State)
    graph.add_node("plan", plan_step)
    graph.add_node("retrieve", retrieve_step)
    graph.add_node("answer", answer_step)
    graph.set_entry_point("plan")
    graph.add_edge("plan", "retrieve")
    graph.add_edge("retrieve", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


def run_spike(n_runs: int = 5) -> str:
    app = build_graph()
    for i in range(n_runs):
        app.invoke({"question": f"question #{i}", "plan": "", "answer": ""})
    if os.environ.get("AIGHT_API_KEY"):
        push(COST_PROCESSOR.buckets)
    return COST_PROCESSOR.report()


if __name__ == "__main__":
    print(run_spike())
