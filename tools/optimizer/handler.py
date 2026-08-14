"""Design Optimization tool — ``run_optimization`` Lambda handler.

Implements the ``run_optimization`` MCP tool described in the design document
("5. Optimization Agent") and registered as an AgentCore Gateway Lambda target.
It executes multi-objective optimization with pymoo's NSGA-II, evaluating
candidate designs through a physics predictor hierarchy:

1. **Bar-Cohen analytical model** (local, fast) — natural-convection heat-sink
   correlations (Bar-Cohen & Rohsenow composite Nusselt number). Primary model
   and universal fallback (Requirement 5.3).
2. **SageMaker endpoint** (optional, cloud) — MLSimKit surrogate. Used only
   when ``SAGEMAKER_ENDPOINT_NAME`` is configured and the requested
   ``model_type`` selects it. If the endpoint does not respond within a
   5-second timeout the predictor automatically falls back to the local
   analytical model for the remainder of the run (Requirement 5.3).

Behaviour (Requirements 5.2, 5.4, 5.8):

* Evaluation budget is validated/clamped to [100, 10000] (default 1000). The
  number of NSGA-II generations is derived so total evaluations never exceed
  the budget.
* A 3-minute wall-clock time budget terminates the search early and returns the
  best-so-far Pareto set with a ``time_budget_exceeded`` notification flag.
* Returns up to 20 Pareto-optimal designs, each with numeric objective values
  and a pass/fail status per constraint.

Configuration is provided entirely through environment variables (no
hardcoding):

* ``SAGEMAKER_ENDPOINT_NAME``     — surrogate endpoint name (enables cloud path)
* ``SAGEMAKER_REGION`` / ``AWS_REGION`` — region for the SageMaker client
* ``SAGEMAKER_TIMEOUT_SECONDS``   — endpoint timeout before local fallback (5)
* ``OPT_DEFAULT_BUDGET``          — default evaluation budget (1000)
* ``OPT_DEFAULT_POPULATION_SIZE`` — default NSGA-II population size (100)
* ``OPT_TIME_BUDGET_SECONDS``     — wall-clock time budget (180 = 3 minutes)
* ``OPT_MAX_PARETO``              — max Pareto designs returned (20)
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass, field
from typing import Any

import boto3
import numpy as np

logger = logging.getLogger(__name__)

# --- Budget / configuration constants (Requirement 5.2) ---------------------

MIN_BUDGET = 100
MAX_BUDGET = 10000
DEFAULT_BUDGET = 1000
DEFAULT_POPULATION_SIZE = 100
DEFAULT_TIME_BUDGET_SECONDS = 180  # 3 minutes (Requirement 5.2 / 5.8)
DEFAULT_SAGEMAKER_TIMEOUT_SECONDS = 5.0  # Requirement 5.3
MAX_PARETO_DESIGNS = 20  # Requirement 5.4 / design "up to 20"

_LOCAL_MODEL_NAME = "bar_cohen_analytical"
_SAGEMAKER_MODEL_NAME = "sagemaker_surrogate"

# model_type values that select the cloud surrogate (else local analytical).
_SAGEMAKER_MODEL_TYPES = frozenset(
    {"sagemaker", "surrogate", "mlsimkit", "thermal_surrogate", "auto"}
)


# --- Result / error types ----------------------------------------------------


class OptimizationConfigError(Exception):
    """Raised when the optimization request is structurally invalid."""

    def __init__(self, message: str, code: str = "invalid_config") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class OptimizationResult:
    """Result of an NSGA-II optimization run (see design "Optimization Result Model").

    Extended with fields required by Requirements 5.3/5.8: the predictor used,
    a fallback flag, the time-budget notification flag, and a human-readable
    notification message.
    """

    pareto_designs: list[dict]  # Parameter values for each design
    pareto_objectives: list[dict]  # Objective values per design (numeric)
    pareto_constraints: list[dict]  # Per-constraint pass/fail per design
    n_evaluations: int  # Total function evaluations (<= budget)
    convergence_history: list[dict]  # Per-generation best objective
    top_recommendations: list[dict]  # Top designs (<=5)
    execution_time_s: float
    source_drawing_id: str  # Traceability
    extraction_id: str  # Traceability
    budget: int
    population_size: int
    generations: int
    predictor_used: str
    fell_back_to_local: bool = False
    time_budget_exceeded: bool = False
    feasible: bool = True
    notification: str = ""
    notifications: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- Objective / constraint parsing -----------------------------------------


@dataclass(frozen=True)
class _Objective:
    """A parsed objective: sense ("maximize"/"minimize") applied to ``field``."""

    sense: str
    field: str

    @property
    def key(self) -> str:
        return f"{self.sense}:{self.field}"


def _parse_objectives(objectives: list[str]) -> list[_Objective]:
    """Parse ``["maximize:heat_dissipation_W", "minimize:weight_kg"]`` forms."""
    if not objectives:
        raise OptimizationConfigError("At least one objective is required.")
    parsed: list[_Objective] = []
    for raw in objectives:
        if not isinstance(raw, str) or ":" not in raw:
            raise OptimizationConfigError(
                f"Objective '{raw}' must be formatted as 'maximize:<field>' or "
                "'minimize:<field>'."
            )
        sense, _, fieldname = raw.partition(":")
        sense = sense.strip().lower()
        fieldname = fieldname.strip()
        if sense not in ("maximize", "minimize") or not fieldname:
            raise OptimizationConfigError(
                f"Objective '{raw}' must be formatted as 'maximize:<field>' or "
                "'minimize:<field>'."
            )
        parsed.append(_Objective(sense=sense, field=fieldname))
    return parsed


@dataclass(frozen=True)
class _DesignVar:
    """A continuous design variable with inclusive [lower, upper] bounds."""

    name: str
    lower: float
    upper: float
    is_int: bool


def _parse_design_space(design_space: dict[str, dict]) -> list[_DesignVar]:
    """Parse ``{param: {"min": .., "max": .., "type": "float"|"int"}}``."""
    if not design_space:
        raise OptimizationConfigError("design_space must define at least one parameter.")
    variables: list[_DesignVar] = []
    for name, spec in design_space.items():
        if not isinstance(spec, dict):
            raise OptimizationConfigError(f"design_space['{name}'] must be an object.")
        lower = spec.get("min", spec.get("lower"))
        upper = spec.get("max", spec.get("upper"))
        if lower is None or upper is None:
            raise OptimizationConfigError(
                f"design_space['{name}'] must define numeric 'min' and 'max' bounds."
            )
        try:
            lower_f = float(lower)
            upper_f = float(upper)
        except (TypeError, ValueError) as exc:
            raise OptimizationConfigError(
                f"design_space['{name}'] bounds must be numeric."
            ) from exc
        if upper_f < lower_f:
            raise OptimizationConfigError(
                f"design_space['{name}'] max ({upper_f}) must be >= min ({lower_f})."
            )
        is_int = str(spec.get("type", "float")).lower() in ("int", "integer")
        variables.append(_DesignVar(name=name, lower=lower_f, upper=upper_f, is_int=is_int))
    return variables


def _constraint_field(name: str, spec: dict) -> str:
    return str(spec.get("field", name))


def _constraint_type(spec: dict) -> str:
    return str(spec.get("type", "max")).lower()


def _constraint_violation(value: float, spec: dict) -> float:
    """Return g(x) where g <= 0 means the constraint is satisfied.

    Supported ``type`` values:
      * ``max`` / ``leq`` / ``upper`` — value must be <= limit
      * ``min`` / ``geq`` / ``lower`` — value must be >= limit
      * ``eq``                        — value must equal limit (within tol)
    """
    ctype = _constraint_type(spec)
    limit = float(spec.get("limit", spec.get("value", 0.0)))
    if ctype in ("min", "geq", "lower", "ge"):
        return limit - value
    if ctype in ("eq", "equal", "equals"):
        tol = float(spec.get("tolerance", spec.get("tol", 1e-6)))
        return abs(value - limit) - tol
    # Default: upper bound.
    return value - limit


def _constraint_satisfied(value: float, spec: dict) -> bool:
    return _constraint_violation(value, spec) <= 1e-9


# --- Physics predictor hierarchy (Requirement 5.3) --------------------------

# Air properties near 300 K (SI units) for natural-convection correlations.
_AIR_K = 0.0263  # thermal conductivity, W/(m*K)
_AIR_NU = 1.59e-5  # kinematic viscosity, m^2/s
_AIR_PR = 0.71  # Prandtl number
_AIR_BETA = 1.0 / 300.0  # thermal expansion coefficient, 1/K
_G = 9.81  # gravitational acceleration, m/s^2

# Solid material properties: (thermal conductivity W/mK, density kg/m^3).
_MATERIALS: dict[str, tuple[float, float]] = {
    "aluminum": (205.0, 2700.0),
    "aluminium": (205.0, 2700.0),
    "aluminum_6061": (167.0, 2700.0),
    "copper": (401.0, 8960.0),
    "steel": (50.0, 7850.0),
    "stainless_steel": (16.0, 8000.0),
    "titanium": (21.9, 4500.0),
    "brass": (109.0, 8500.0),
}
_DEFAULT_MATERIAL = ("aluminum", 205.0, 2700.0)


def _num(params: dict[str, Any], *keys: str, default: float) -> float:
    """Return the first numeric value found under ``keys`` (else ``default``)."""
    for key in keys:
        if key in params and params[key] is not None:
            try:
                return float(params[key])
            except (TypeError, ValueError):
                continue
    return default


def _material_properties(params: dict[str, Any]) -> tuple[float, float]:
    """Resolve (k_solid, density) from a material name or explicit params."""
    raw = params.get("material")
    if isinstance(raw, str):
        props = _MATERIALS.get(raw.strip().lower().replace(" ", "_"))
        if props is not None:
            k, rho = props
        else:
            k, rho = _DEFAULT_MATERIAL[1], _DEFAULT_MATERIAL[2]
    else:
        k, rho = _DEFAULT_MATERIAL[1], _DEFAULT_MATERIAL[2]
    # Explicit overrides win when present.
    k = _num(params, "thermal_conductivity", "k_solid", default=k)
    rho = _num(params, "density", "density_kg_m3", "rho", default=rho)
    return k, rho


def bar_cohen_predict(params: dict[str, Any]) -> dict[str, float]:
    """Bar-Cohen analytical natural-convection heat-sink model.

    Computes derived thermal/mechanical metrics from geometric + material
    design parameters using the Bar-Cohen & Rohsenow composite Nusselt-number
    correlation for an array of vertical parallel plates (fins). All inputs
    are defensively defaulted so the model returns numeric metrics for any
    design space (Requirement 5.3: analytical model is the universal
    fallback).

    Returns a metrics dict that always echoes the raw design parameters and
    adds derived quantities: ``heat_dissipation_W``, ``weight_kg``,
    ``thermal_resistance_KpW``, ``surface_temperature_C``, ``volume_m3``,
    ``fin_spacing_m``, ``heat_transfer_coefficient``.
    """
    metrics: dict[str, float] = {}
    # Echo raw numeric params so objectives/constraints can reference them.
    for key, value in params.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[key] = float(value)

    # Geometry (metres) with sensible defaults for a small heat sink.
    length = max(_num(params, "length_m", "length", "L", default=0.1), 1e-4)
    width = max(_num(params, "width_m", "width", "W", default=0.1), 1e-4)
    fin_height = max(
        _num(params, "fin_height_m", "fin_height", "height_m", "height", "H", default=0.02),
        1e-4,
    )
    fin_thickness = max(
        _num(params, "fin_thickness_m", "fin_thickness", "t_fin", default=0.001), 1e-5
    )
    base_thickness = max(
        _num(params, "base_thickness_m", "base_thickness", "t_base", default=0.003), 1e-5
    )
    n_fins = max(int(round(_num(params, "fin_count", "n_fins", "fins", default=10))), 1)
    ambient_c = _num(params, "ambient_temp_C", "ambient_C", "T_ambient", default=25.0)
    delta_t = max(_num(params, "delta_T", "temp_rise_K", "dT", default=40.0), 1e-3)

    k_solid, density = _material_properties(params)

    # Fin spacing from the channel width (m). Guard against degenerate values.
    if n_fins > 1:
        spacing = (width - n_fins * fin_thickness) / (n_fins - 1)
    else:
        spacing = width
    spacing = max(spacing, 5e-4)

    # Bar-Cohen & Rohsenow composite Nusselt number for the fin channel.
    alpha = _AIR_NU / _AIR_PR  # thermal diffusivity of air, m^2/s
    ra_s = (_G * _AIR_BETA * delta_t * spacing**3) / (_AIR_NU * alpha)
    elenbaas = max(ra_s * (spacing / length), 1e-6)
    nu_s = (576.0 / elenbaas**2 + 2.873 / math.sqrt(elenbaas)) ** -0.5
    h = nu_s * _AIR_K / spacing  # convective coefficient, W/(m^2*K)

    # Heat-transfer surface area: both faces of every fin plus the base.
    fin_area = 2.0 * n_fins * fin_height * length
    base_area = width * length
    total_area = fin_area + base_area

    heat_dissipation = h * total_area * delta_t  # W
    volume = base_area * base_thickness + n_fins * (length * fin_height * fin_thickness)
    weight = volume * density  # kg
    thermal_resistance = delta_t / heat_dissipation if heat_dissipation > 0 else 1e6
    surface_temp = ambient_c + delta_t

    metrics.update(
        {
            "heat_dissipation_W": float(heat_dissipation),
            "weight_kg": float(weight),
            "volume_m3": float(volume),
            "thermal_resistance_KpW": float(thermal_resistance),
            "surface_temperature_C": float(surface_temp),
            "fin_spacing_m": float(spacing),
            "heat_transfer_coefficient": float(h),
            "surface_area_m2": float(total_area),
            "fin_count": float(n_fins),
            # Convenience: material thermal conductivity for spreading resistance.
            "material_conductivity_WmK": float(k_solid),
        }
    )
    return metrics


# --- SageMaker client (isolated getter for testability) ----------------------


def get_sagemaker_client() -> Any:
    """Return a SageMaker runtime client. Isolated so unit tests can patch it."""
    region = os.environ.get("SAGEMAKER_REGION") or os.environ.get("AWS_REGION")
    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    return boto3.client("sagemaker-runtime", **kwargs)


def _sagemaker_timeout() -> float:
    raw = os.environ.get("SAGEMAKER_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_SAGEMAKER_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_SAGEMAKER_TIMEOUT_SECONDS


class PhysicsPredictor:
    """Physics predictor implementing the local/cloud hierarchy (Req 5.3).

    The predictor prefers the SageMaker surrogate endpoint when it is
    configured and selected by ``model_type``. Each cloud call is bounded by a
    5-second timeout (configurable via ``SAGEMAKER_TIMEOUT_SECONDS``); on
    timeout or any error the predictor permanently falls back to the local
    Bar-Cohen analytical model for the rest of the run and records the
    fallback.
    """

    def __init__(self, model_type: str = "thermal_analytical") -> None:
        self.model_type = (model_type or "thermal_analytical").strip().lower()
        endpoint = os.environ.get("SAGEMAKER_ENDPOINT_NAME")
        self.endpoint_name = endpoint or None
        self._sagemaker_enabled = bool(endpoint) and self.model_type in _SAGEMAKER_MODEL_TYPES
        self._timeout = _sagemaker_timeout()
        self.fell_back_to_local = False
        self.used_sagemaker = False

    @property
    def predictor_used(self) -> str:
        return _SAGEMAKER_MODEL_NAME if self.used_sagemaker else _LOCAL_MODEL_NAME

    def predict(self, params: dict[str, Any]) -> dict[str, float]:
        if self._sagemaker_enabled:
            try:
                metrics = self._predict_sagemaker(params)
                self.used_sagemaker = True
                return metrics
            except Exception as exc:  # timeout, throttling, parse error, etc.
                logger.warning(
                    "SageMaker endpoint '%s' failed (%s); falling back to local "
                    "Bar-Cohen analytical model for the remainder of the run.",
                    self.endpoint_name,
                    exc,
                )
                self._sagemaker_enabled = False
                self.fell_back_to_local = True
        return bar_cohen_predict(params)

    def _predict_sagemaker(self, params: dict[str, Any]) -> dict[str, float]:
        """Invoke the SageMaker surrogate endpoint with a hard timeout.

        A worker thread performs the (potentially slow) ``invoke_endpoint``
        call; ``future.result(timeout=...)`` enforces the 5-second budget so a
        hanging endpoint cannot block the optimization (Requirement 5.3).
        """
        payload = json.dumps({"inputs": params}).encode("utf-8")

        def _invoke() -> dict[str, Any]:
            client = get_sagemaker_client()
            response = client.invoke_endpoint(
                EndpointName=self.endpoint_name,
                ContentType="application/json",
                Body=payload,
            )
            body = response["Body"]
            raw = body.read() if hasattr(body, "read") else body
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            return json.loads(raw)

        # Note: do NOT use ThreadPoolExecutor as a context manager here — its
        # __exit__ calls shutdown(wait=True), which would block on a hung
        # endpoint and defeat the timeout. Shut down without waiting so the
        # 5-second budget is strictly enforced (Requirement 5.3).
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_invoke)
        try:
            result = future.result(timeout=self._timeout)
        except FutureTimeoutError as exc:
            executor.shutdown(wait=False)
            raise TimeoutError(
                f"SageMaker endpoint did not respond within {self._timeout}s"
            ) from exc
        executor.shutdown(wait=False)

        metrics = result.get("metrics", result) if isinstance(result, dict) else {}
        parsed: dict[str, float] = {}
        for key, value in (metrics or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                parsed[key] = float(value)
        # Merge raw params so objective/constraint fields resolve even when the
        # surrogate only returns a subset of derived metrics.
        for key, value in params.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                parsed.setdefault(key, float(value))
        if not parsed:
            raise ValueError("SageMaker response contained no numeric metrics")
        return parsed


# --- Metric lookup / objective + constraint evaluation ----------------------


def _metric_value(metrics: dict[str, float], params: dict[str, Any], field_name: str) -> float:
    """Resolve a field: prefer derived metrics, then raw params, else 0.0."""
    if field_name in metrics:
        return float(metrics[field_name])
    if field_name in params and isinstance(params[field_name], (int, float)):
        return float(params[field_name])
    return 0.0


def _objective_values(
    metrics: dict[str, float], params: dict[str, Any], objectives: list[_Objective]
) -> dict[str, float]:
    return {obj.key: _metric_value(metrics, params, obj.field) for obj in objectives}


def _constraint_statuses(
    metrics: dict[str, float], params: dict[str, Any], constraints: dict[str, dict]
) -> dict[str, bool]:
    statuses: dict[str, bool] = {}
    for cname, spec in constraints.items():
        value = _metric_value(metrics, params, _constraint_field(cname, spec))
        statuses[cname] = _constraint_satisfied(value, spec)
    return statuses


# --- Budget / population validation (Requirement 5.2) -----------------------


def _default_budget() -> int:
    return _env_int("OPT_DEFAULT_BUDGET", DEFAULT_BUDGET)


def _default_population() -> int:
    return _env_int("OPT_DEFAULT_POPULATION_SIZE", DEFAULT_POPULATION_SIZE)


def _time_budget() -> float:
    raw = os.environ.get("OPT_TIME_BUDGET_SECONDS")
    if not raw:
        return float(DEFAULT_TIME_BUDGET_SECONDS)
    try:
        return float(raw)
    except ValueError:
        return float(DEFAULT_TIME_BUDGET_SECONDS)


def _max_pareto() -> int:
    return _env_int("OPT_MAX_PARETO", MAX_PARETO_DESIGNS)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def clamp_budget(budget: int | None) -> int:
    """Clamp the evaluation budget to [MIN_BUDGET, MAX_BUDGET] (Requirement 5.2)."""
    if budget is None:
        budget = _default_budget()
    try:
        value = int(budget)
    except (TypeError, ValueError):
        value = _default_budget()
    if value < MIN_BUDGET:
        return MIN_BUDGET
    if value > MAX_BUDGET:
        return MAX_BUDGET
    return value


def clamp_population(population_size: int | None, budget: int) -> int:
    """Clamp population to a sane range: [2, budget]."""
    if population_size is None:
        population_size = _default_population()
    try:
        value = int(population_size)
    except (TypeError, ValueError):
        value = _default_population()
    value = max(2, value)
    return min(value, budget)


# --- NSGA-II optimization core (Requirements 5.2, 5.4, 5.8) -----------------


def run_optimization(
    objectives: list[str],
    design_space: dict[str, dict],
    constraints: dict[str, dict] | None = None,
    budget: int = DEFAULT_BUDGET,
    population_size: int = DEFAULT_POPULATION_SIZE,
    model_type: str = "thermal_analytical",
    source_drawing_id: str = "",
    extraction_id: str = "",
) -> OptimizationResult:
    """Execute NSGA-II multi-objective optimization and return a Pareto set.

    Args:
        objectives: ``["maximize:<field>", "minimize:<field>", ...]``.
        design_space: ``{param: {"min", "max", "type"}}`` bounds.
        constraints: ``{name: {"field", "type", "limit"}}`` (optional).
        budget: Evaluation budget, clamped to [100, 10000] (default 1000).
        population_size: NSGA-II population size (default 100).
        model_type: Physics model selector (``thermal_analytical`` local, or a
            surrogate type to use the SageMaker endpoint when configured).
        source_drawing_id / extraction_id: Traceability identifiers.

    Returns:
        OptimizationResult with up to 20 Pareto designs (each with numeric
        objective values and per-constraint pass/fail status), total
        evaluations (<= budget), convergence history, timing, and the
        ``time_budget_exceeded`` notification flag.
    """
    # Deferred import so unit tests that only exercise parsing/validation do
    # not require pymoo, and so import errors surface at call time.
    from pymoo.algorithms.moo.nsga2 import NSGA2
    from pymoo.core.problem import ElementwiseProblem
    from pymoo.termination.max_gen import MaximumGenerationTermination
    from pymoo.util.nds.non_dominated_sorting import NonDominatedSorting

    constraints = constraints or {}
    parsed_objectives = _parse_objectives(objectives)
    variables = _parse_design_space(design_space)

    budget = clamp_budget(budget)
    population_size = clamp_population(population_size, budget)
    generations = max(1, budget // population_size)
    time_budget = _time_budget()

    predictor = PhysicsPredictor(model_type=model_type)
    n_var = len(variables)
    n_obj = len(parsed_objectives)
    n_constr = len(constraints)
    xl = np.array([v.lower for v in variables], dtype=float)
    xu = np.array([v.upper for v in variables], dtype=float)

    def _params_from_x(x: np.ndarray) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for i, var in enumerate(variables):
            value = float(x[i])
            params[var.name] = int(round(value)) if var.is_int else value
        return params

    eval_counter = {"n": 0}

    class _DesignProblem(ElementwiseProblem):
        def __init__(self) -> None:
            super().__init__(
                n_var=n_var,
                n_obj=n_obj,
                n_ieq_constr=n_constr,
                xl=xl,
                xu=xu,
            )

        def _evaluate(self, x: np.ndarray, out: dict[str, Any], *args: Any, **kwargs: Any) -> None:
            eval_counter["n"] += 1
            params = _params_from_x(x)
            metrics = predictor.predict(params)
            # pymoo minimizes: negate maximize objectives.
            f = []
            for obj in parsed_objectives:
                value = _metric_value(metrics, params, obj.field)
                f.append(-value if obj.sense == "maximize" else value)
            out["F"] = np.array(f, dtype=float)
            if n_constr:
                g = [
                    _constraint_violation(
                        _metric_value(metrics, params, _constraint_field(cname, spec)), spec
                    )
                    for cname, spec in constraints.items()
                ]
                out["G"] = np.array(g, dtype=float)

    problem = _DesignProblem()
    algorithm = NSGA2(pop_size=population_size)
    algorithm.setup(
        problem,
        termination=MaximumGenerationTermination(generations),
        seed=1,
        verbose=False,
    )

    start = time.monotonic()
    time_budget_exceeded = False
    convergence_history: list[dict] = []

    while algorithm.has_next():
        algorithm.next()
        elapsed = time.monotonic() - start
        # Record per-generation best (minimized) objective for each objective.
        pop_f = algorithm.pop.get("F")
        if pop_f is not None and len(pop_f) > 0:
            best = pop_f.min(axis=0)
            convergence_history.append(
                {
                    "generation": int(algorithm.n_gen),
                    "n_evaluations": int(eval_counter["n"]),
                    "best_objectives": {
                        obj.key: float(-best[i] if obj.sense == "maximize" else best[i])
                        for i, obj in enumerate(parsed_objectives)
                    },
                }
            )
        if elapsed >= time_budget:
            time_budget_exceeded = True  # Requirement 5.8
            break

    execution_time = time.monotonic() - start

    # Gather the final population and extract the non-dominated (Pareto) front.
    pop = algorithm.pop
    x_all = np.atleast_2d(pop.get("X"))
    f_all = np.atleast_2d(pop.get("F"))
    if n_constr:
        cv_all = np.asarray(pop.get("CV")).reshape(-1)
    else:
        cv_all = np.zeros(len(x_all))

    feasible_idx = np.where(cv_all <= 1e-9)[0]
    is_feasible = feasible_idx.size > 0
    candidate_idx = feasible_idx if is_feasible else np.arange(len(x_all))

    fronts = NonDominatedSorting().do(f_all[candidate_idx], only_non_dominated_front=True)
    pareto_idx = candidate_idx[fronts]

    max_pareto = _max_pareto()
    # Deduplicate identical design rows while preserving order.
    seen: set[tuple] = set()
    ordered_idx: list[int] = []
    for idx in pareto_idx:
        key = tuple(np.round(x_all[idx], 9).tolist())
        if key not in seen:
            seen.add(key)
            ordered_idx.append(int(idx))
    ordered_idx = ordered_idx[:max_pareto]

    pareto_designs: list[dict] = []
    pareto_objectives: list[dict] = []
    pareto_constraints: list[dict] = []
    for idx in ordered_idx:
        params = _params_from_x(x_all[idx])
        # Re-evaluate with the local analytical model for a stable, sign-correct
        # readout of objective values and per-constraint status.
        metrics = bar_cohen_predict(params)
        pareto_designs.append(params)
        pareto_objectives.append(_objective_values(metrics, params, parsed_objectives))
        pareto_constraints.append(_constraint_statuses(metrics, params, constraints))

    top_recommendations = [
        {
            "design": pareto_designs[i],
            "objectives": pareto_objectives[i],
            "constraints": pareto_constraints[i],
        }
        for i in range(min(5, len(pareto_designs)))
    ]

    notifications: list[str] = []
    if time_budget_exceeded:
        notifications.append(
            f"Time budget of {int(time_budget)}s exceeded; returning the best "
            "Pareto-optimal set found so far."
        )
    if predictor.fell_back_to_local:
        notifications.append(
            "SageMaker endpoint did not respond within the timeout; used the "
            "local Bar-Cohen analytical model."
        )
    if not is_feasible and n_constr:
        notifications.append(
            "No fully feasible designs were found; returning the least-infeasible "
            "Pareto set. Consider relaxing constraints."
        )

    return OptimizationResult(
        pareto_designs=pareto_designs,
        pareto_objectives=pareto_objectives,
        pareto_constraints=pareto_constraints,
        n_evaluations=int(eval_counter["n"]),
        convergence_history=convergence_history,
        top_recommendations=top_recommendations,
        execution_time_s=float(execution_time),
        source_drawing_id=str(source_drawing_id or ""),
        extraction_id=str(extraction_id or ""),
        budget=budget,
        population_size=population_size,
        generations=generations,
        predictor_used=predictor.predictor_used,
        fell_back_to_local=predictor.fell_back_to_local,
        time_budget_exceeded=time_budget_exceeded,
        feasible=bool(is_feasible or n_constr == 0),
        notification="; ".join(notifications),
        notifications=notifications,
    )


# --- HTTP / MCP helpers ------------------------------------------------------


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _extract_args(event: dict[str, Any]) -> dict[str, Any]:
    """Extract tool arguments from an MCP/Lambda event or an HTTP body.

    AgentCore Gateway Lambda targets typically pass the tool arguments as the
    event payload directly; HTTP/direct invocations may wrap them in ``body``.
    """
    if "objectives" in event or "design_space" in event:
        return event
    raw = event.get("body")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# --- Lambda / MCP entry point ------------------------------------------------


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for the ``run_optimization`` MCP tool target."""
    event = event or {}
    args = _extract_args(event)

    try:
        result = run_optimization(
            objectives=args.get("objectives") or [],
            design_space=args.get("design_space") or {},
            constraints=args.get("constraints") or {},
            budget=args.get("budget", DEFAULT_BUDGET),
            population_size=args.get("population_size", DEFAULT_POPULATION_SIZE),
            model_type=args.get("model_type", "thermal_analytical"),
            source_drawing_id=args.get("source_drawing_id", ""),
            extraction_id=args.get("extraction_id", ""),
        )
    except OptimizationConfigError as exc:
        return _response(400, {"status": "error", "code": exc.code, "message": exc.message})
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Unhandled error in run_optimization handler")
        return _response(500, {"status": "error", "message": f"Internal error: {exc}"})

    return _response(200, {"status": "ok", "result": result.to_dict()})


__all__ = [
    "MIN_BUDGET",
    "MAX_BUDGET",
    "DEFAULT_BUDGET",
    "DEFAULT_POPULATION_SIZE",
    "DEFAULT_TIME_BUDGET_SECONDS",
    "MAX_PARETO_DESIGNS",
    "OptimizationConfigError",
    "OptimizationResult",
    "PhysicsPredictor",
    "bar_cohen_predict",
    "clamp_budget",
    "clamp_population",
    "get_sagemaker_client",
    "run_optimization",
    "handler",
]
