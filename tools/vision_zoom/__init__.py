"""`zoom_and_read` Lambda tool package.

Exposes the high-resolution drawing-region reader used by the QA and
Extraction agents (see design section "7. AgentCore Gateway" — tool
``zoom_and_read``). Crops a drawing region at 400 DPI and reads it with a
Claude Sonnet vision model, returning structured dimensional data.
"""
