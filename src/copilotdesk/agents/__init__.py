"""Pure stage logic used by the pipeline filters, plus the offline eval harness.

Nothing here knows about envelopes, tracing or ordering - the topology lives in
``copilotdesk.pipeline``. These modules are plain functions so they stay
independently testable and reusable by any filter that wants them.
"""
