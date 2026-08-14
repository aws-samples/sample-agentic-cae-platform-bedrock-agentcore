"""Compliance tool package — ``check_compliance`` Lambda target.

Validates a Ground_Truth_JSON document against engineering standards
(ISO 2768 general tolerances, GD&T completeness, and an OEM baseline rule
set) and returns pass/fail per standard with specific violation details.

Exposed via the Bedrock AgentCore Gateway as the MCP tool ``check_compliance``
(see design section "7. AgentCore Gateway", tool ``check_compliance``).
"""
