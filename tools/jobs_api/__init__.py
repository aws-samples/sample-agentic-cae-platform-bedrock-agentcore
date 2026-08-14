"""Async jobs HTTP API package (async-rest-sse-migration task 1.6).

Exposes the ``start_job`` / ``job_status`` handlers that front the JobsTable and
dispatch the async ``jobs_worker`` Lambda. See :mod:`tools.jobs_api.handler`.
"""
