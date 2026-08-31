"""The audit that runs between the query result and the story told about it.

An answer arrives with rows attached; the auditor decides which cross-checks can
settle the claims that answer is about to make (``planner``), runs them against
the warehouse on one read-only connection (``executor``), and reuses the
population figures they need across requests (``memory``).
"""
