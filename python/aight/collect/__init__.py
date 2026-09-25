"""Collectors for agents the customer runs but did not write.

Each reads an agent's own on-disk record of the calls it made and pushes one
row per step. Unlike the SDKs these instrument nothing — there is no customer
code on the stack to bind to, which is why the chain they push is a step path
rather than a source path. See docs/hub/agents.md external-agent section.
"""
