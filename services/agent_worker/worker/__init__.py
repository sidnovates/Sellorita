"""Sellorita agent worker: Celery tasks running the LangGraph multi-agent pipeline.

Layout:
    celery_app.py  Celery app + Beat schedule
    tasks.py       run_campaign_pipeline, schedule_due_campaigns
    graph/         LangGraph state machine wiring
    agents/        one module per pipeline layer (intelligence, creative, production, qa)
    renderers/     media rendering (mock FFmpeg now; HyperFrames / Playwright in Phase 2)
    cache.py       Redis cache for intelligence results
"""
