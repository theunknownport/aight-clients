"""A stand-in for "a helper module the agent calls into".

Deliberately a real second file: report_value() must attribute value to the
*outer* agent file, the same one traced_llm_call attributes spend to, and a
single file cannot tell those two resolutions apart. See
test_tracing.test_report_value_attributes_to_the_outer_agent_not_the_helper.
"""
from aight.tracing import report_value


def report_from_helper(value_usd: float) -> None:
    report_value(value_usd)
