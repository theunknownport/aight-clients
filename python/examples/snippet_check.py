"""Minimal script to sanity-check source-snippet capture end to end.

No framework dependency (unlike langgraph_spike.py) — just a few
traced_llm_call() sites at known lines, so you can eyeball that SNIPPETS
picked up the right +/-3 lines around each one.

Run: python -m examples.snippet_check
Set AIGHT_API_KEY first to also push the result to your AIght Workspace and
check the Line Review panel renders the snippet instead of the
"not captured yet" fallback.
"""
import os

from aight.remote import push
from aight.tracing import COST_PROCESSOR, SNIPPETS, traced_llm_call


def plan_step():
    traced_llm_call("gpt-4o-mini", input_tokens=180, output_tokens=60)


def retrieve_step():
    # Deliberately expensive: simulates re-embedding the full corpus.
    traced_llm_call("gpt-4o-mini", input_tokens=900, output_tokens=40)


def answer_step():
    traced_llm_call("gpt-4o", input_tokens=400, output_tokens=250)


def run():
    for _ in range(3):
        plan_step()
        retrieve_step()
        answer_step()

    print(COST_PROCESSOR.report())
    print(f"\n{len(SNIPPETS)} unique (file, line) snippets captured:\n")
    for (filepath, lineno), (start, text) in SNIPPETS.items():
        print(f"--- {os.path.basename(filepath)}:{lineno} (from line {start}) ---")
        print(text)
        print()

    if os.environ.get("AIGHT_API_KEY"):
        push(COST_PROCESSOR.buckets)
        print("Pushed to the AIght Workspace — check Line Review for these call sites.")
    else:
        print("AIGHT_API_KEY not set — skipped push, snippets only verified locally.")


if __name__ == "__main__":
    run()
