"""`run_optimization` AgentCore Gateway Lambda tool target.

Multi-objective design optimization with pymoo NSGA-II and a Bar-Cohen
analytical thermal physics predictor (local), with an optional SageMaker
surrogate-model endpoint that falls back to the local model on a 5-second
timeout.
"""
