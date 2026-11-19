"""Sellorita agent worker: Celery tasks running the LangGraph multi-agent pipeline.

Layout:
    graph/      LangGraph state machine wiring (Step 4)
    agents/     one module per agent node (Step 4+)
    renderers/  HTML -> PNG / MP4 rendering helpers (Step 5)
"""
