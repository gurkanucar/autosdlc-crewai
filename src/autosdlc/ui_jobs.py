"""Holds the start-up errors of background jobs while Streamlit reruns."""

WORKER_ERRORS: dict[str, str] = {}
# Runs whose worker thread is alive. A resumed run stays "paused" until its worker has loaded and claimed it, which
# takes a few seconds; offering Resume in that window started a second worker that failed with "already being resumed".
ACTIVE_JOBS: set[str] = set()
