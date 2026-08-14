"""Property-based tests for the optimization tools (design "5. Optimization Agent").

Covers two correctness properties from the design document:

- **Property 6: Constraint Parsing from Ground Truth JSON** — For any *valid*
  Ground_Truth_JSON document containing dimensional, material, and tolerance
  data, ``validate_constraints`` SHALL correctly parse and extract these as
  design constraints: numeric dimensional bounds (nominal ± tolerance),
  material-derived limits (known vs. unknown), and tolerance ranges (ISO 2768
  general tolerance + GD&T callout zones), reporting present/missing
  constraint categories.
  **Validates: Requirements 5.1**

- **Property 7: NSGA-II Produces Valid Pareto Results** — For any valid
  optimization configuration (objectives, design space, optional constraints,
  budget in [100, 10000]), ``run_optimization`` SHALL execute NSGA-II and
  return Pareto-optimal designs where each design has numeric objective values,
  a pass/fail status per constraint, and the total evaluations do not exceed
  the (clamped) budget, with at most 20 designs returned.
  **Validates: Requirements 5.2, 5.4**

The generators emit only SCHEMA-VALID Ground_Truth_JSON (Property 6) and
structurally valid configs (Property 7). Expected constraint values are
recomputed independently of the handler's parsing helpers so the tests do not
merely restate the implementation. Budgets/populations are kept small so the
NSGA-II runs finish quickly and the Hypothesis example counts are bounded.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shared.validators.schema_validator import validate_ground_truth
from tools.constraint_validator import handler as cv
from tools.optimizer import handler as opt

# ===========================================================================
# Property 6: Constraint Parsing from Ground Truth JSON
# ===========================================================================

DIMENSION_TYPES = ["linear", "diameter", "radius", "angular"]
ISO_CLASSES = ["f", "m", "c", "v"]

# Materials whose raw form maps to a known, documented material key. The
# expected normalized key is fixed here so the test is independent of the
# handler's own alias-matching logic.
KNOWN_MATERIALS: list[tuple[str, str]] = [
    ("Aluminum", "aluminum"),
    ("aluminium", "aluminum"),
    ("Aluminum 6061-T6", "aluminum_6061"),
    ("AL 7075", "aluminum_7075"),
    ("Copper", "copper"),
    ("Steel", "steel"),
    ("Stainless Steel", "stainless_steel"),
    ("Titanium", "titanium"),
    ("Brass", "brass"),
    ("Cast Iron", "cast_iron"),
]

# Materials with no documented properties and no alias-fragment overlap, so
# they must be reported as present-but-unknown (not silently dropped).
UNKNOWN_MATERIALS: list[str] = [
    "Unobtanium", "Vibranium", "Mithril", "Adamantium", "Plastic",
    "Wood", "Rubber", "Glass", "Ceramic", "Kryptonite",
]

# String values the handler treats as "not really specified" (blank).
PLACEHOLDER_MATERIALS = ["N/A", "TBD", "-", "", "none", "unknown"]
# General-tolerance placeholders that carry no ISO 2768 class.
PLACEHOLDER_TOLERANCES = ["TBD", "-", "", "see notes", "general"]


def _fmt(value: float) -> str:
    """Format a clean 2-decimal magnitude without trailing float noise."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


# A magnitude drawn as hundredths keeps string<->float round-tripping exact.
_magnitude = st.integers(min_value=1, max_value=500).map(lambda h: h / 100.0)
_nominal = st.integers(min_value=1, max_value=100_000).map(lambda h: h / 100.0)


@st.composite
def _dimension_spec(draw) -> tuple[dict, dict]:
    """Draw a schema-valid dimension entry plus its independently-expected bounds.

    Returns ``(dimension_dict, expected)`` where ``expected`` holds the nominal
    value and the recomputed ``lower_bound`` / ``upper_bound`` / whether an
    explicit tolerance applies.
    """
    nominal = draw(_nominal)
    dim_type = draw(st.sampled_from(DIMENSION_TYPES))
    feature = draw(st.text(min_size=1, max_size=16))
    case = draw(st.sampled_from(["none", "pm", "slash_pm", "bare", "asymmetric"]))

    if case == "none":
        tolerance = None
        lower, upper, has_tol = nominal, nominal, False
    elif case in ("pm", "slash_pm", "bare"):
        m = draw(_magnitude)
        if case == "pm":
            tolerance = f"±{_fmt(m)}"
        elif case == "slash_pm":
            tolerance = f"+/-{_fmt(m)}"
        else:  # bare unsigned magnitude → symmetric
            tolerance = _fmt(m)
        lower, upper, has_tol = nominal - m, nominal + m, True
    else:  # asymmetric "+p/-n"
        p = draw(_magnitude)
        n = draw(_magnitude)
        tolerance = f"+{_fmt(p)}/-{_fmt(n)}"
        lower, upper, has_tol = nominal - n, nominal + p, True

    dimension = {
        "value": nominal,
        "tolerance": tolerance,
        "type": dim_type,
        "feature": feature,
    }
    expected = {
        "nominal": nominal,
        "lower_bound": lower,
        "upper_bound": upper,
        "has_explicit_tolerance": has_tol,
    }
    return dimension, expected


@st.composite
def _material_choice(draw) -> tuple[str, str | None, bool]:
    """Draw a (raw, expected_normalized, expected_known) material triple."""
    if draw(st.booleans()):
        raw, normalized = draw(st.sampled_from(KNOWN_MATERIALS))
        return raw, normalized, True
    raw = draw(st.sampled_from(UNKNOWN_MATERIALS))
    return raw, None, False


@st.composite
def _gdt_spec(draw) -> tuple[dict, float]:
    """Draw a schema-valid GD&T callout plus its expected total-zone upper bound."""
    signed = draw(st.integers(min_value=-500, max_value=500).filter(lambda h: h != 0))
    tolerance_value = signed / 100.0
    callout = {
        "spec_id": draw(st.text(min_size=1, max_size=8)),
        "characteristic": draw(
            st.sampled_from(["flatness", "position", "perpendicularity", "runout"])
        ),
        "tolerance_value": tolerance_value,
        "unit": draw(st.sampled_from(["mm", "in"])),
        "zone_shape": draw(st.one_of(st.none(), st.just("diameter"))),
        "modifier": draw(st.one_of(st.none(), st.just("MMC"))),
        "datum_refs": draw(st.lists(st.sampled_from(["A", "B", "C"]), max_size=3)),
        "applied_to": draw(st.text(min_size=1, max_size=12)),
    }
    return callout, abs(tolerance_value)


def _bom_entry(index: int, material: str) -> dict:
    return {
        "item": index + 1,
        "part_number": f"PN-{index}",
        "description": f"part-{index}",
        "material": material,
        "qty": 1,
    }


@st.composite
def constraint_document(draw) -> tuple[dict, dict]:
    """Generate a schema-valid Ground_Truth_JSON plus the independently-expected
    constraint extraction (dimensional bounds, material limits, tolerance ranges,
    and present/missing categories)."""
    dim_specs = draw(st.lists(_dimension_spec(), max_size=4))
    dimensions = [d for d, _ in dim_specs]
    expected_bounds = [e for _, e in dim_specs]

    # Title-block material: either a real (present) material or a placeholder.
    material_present = draw(st.booleans())
    if material_present:
        tb_raw, tb_norm, tb_known = draw(_material_choice())
    else:
        tb_raw = draw(st.sampled_from(PLACEHOLDER_MATERIALS))
        tb_norm, tb_known = None, False

    bom_specs = draw(st.lists(_material_choice(), max_size=3))
    bom = [_bom_entry(i, raw) for i, (raw, _, _) in enumerate(bom_specs)]

    # Tolerance category: ISO 2768 general tolerance and/or GD&T callouts.
    iso_present = draw(st.booleans())
    if iso_present:
        iso_class = draw(st.sampled_from(ISO_CLASSES))
        general_tolerance = f"ISO 2768-{iso_class}"
    else:
        iso_class = None
        general_tolerance = draw(st.sampled_from(PLACEHOLDER_TOLERANCES))

    gdt_specs = draw(st.lists(_gdt_spec(), max_size=3))
    gdt_callouts = [c for c, _ in gdt_specs]
    expected_gdt_upper = [u for _, u in gdt_specs]

    document = {
        "drawing_id": draw(st.text(min_size=1, max_size=16)),
        "extraction_timestamp": draw(st.datetimes().map(lambda dt: dt.isoformat())),
        "title_block": {
            "drawing_number": "DRW-1",
            "title": "Part",
            "part_number": "PN-1",
            "revision": "A",
            "material": tb_raw,
            "units": "mm",
            "scale": "1:1",
            "projection": draw(st.sampled_from(["first_angle", "third_angle"])),
            "sheet_size": "A3",
            "general_tolerance": general_tolerance,
            "drawn_by": "eng",
            "date": "2024-01-01",
            "approved_by": "lead",
            "weight": "1 kg",
        },
        "bom": bom,
        "gdt_callouts": gdt_callouts,
        "dimensions": dimensions,
        "notes": [],
        "revisions": [],
    }

    # Expected material limits (title block first, then bom in order).
    expected_materials: list[dict] = []
    if material_present:
        expected_materials.append(
            {"source": "title_block", "normalized": tb_norm, "known": tb_known}
        )
    for i, (_, norm, known) in enumerate(bom_specs):
        expected_materials.append(
            {"source": f"bom[{i}]", "normalized": norm, "known": known}
        )

    material_category_present = material_present or len(bom_specs) > 0
    tolerance_category_present = iso_present or len(gdt_specs) > 0

    present = []
    missing = []
    (present if dimensions else missing).append(cv.CATEGORY_DIMENSIONAL)
    (present if material_category_present else missing).append(cv.CATEGORY_MATERIAL)
    (present if tolerance_category_present else missing).append(cv.CATEGORY_TOLERANCE)

    expected = {
        "bounds": expected_bounds,
        "materials": expected_materials,
        "iso_class": iso_class,
        "gdt_upper": expected_gdt_upper,
        "present": present,
        "missing": missing,
    }
    return document, expected


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(case=constraint_document())
def test_property_6_constraint_parsing_from_ground_truth(case: tuple) -> None:
    """``validate_constraints`` extracts the correct constraints from any valid doc.

    **Validates: Requirements 5.1**
    """
    document, expected = case

    # The generated document must actually be schema-valid (Property 6 premise).
    schema_result = validate_ground_truth(document)
    assert schema_result.valid, (
        "Generator produced a schema-invalid document: "
        f"{[(e.field_path, e.constraint) for e in schema_result.errors]}"
    )

    result = cv.validate_constraints(document)

    # --- Dimensional bounds: one per dimension, in order, min/max recomputed ---
    assert len(result.dimensional_bounds) == len(expected["bounds"])
    for bound, exp in zip(result.dimensional_bounds, expected["bounds"]):
        assert bound.nominal == pytest.approx(exp["nominal"])
        assert bound.lower_bound == pytest.approx(exp["lower_bound"])
        assert bound.upper_bound == pytest.approx(exp["upper_bound"])
        assert bound.has_explicit_tolerance == exp["has_explicit_tolerance"]
        # Bounds are ordered (lower <= upper) and unit is carried from title block.
        assert bound.lower_bound <= bound.upper_bound
        assert bound.unit == "mm"

    # --- Material limits: known vs. unknown resolution, none dropped ----------
    actual_materials = {m.source: m for m in result.material_limits}
    assert len(result.material_limits) == len(expected["materials"])
    for exp in expected["materials"]:
        limit = actual_materials[exp["source"]]
        assert limit.known == exp["known"]
        assert limit.normalized == exp["normalized"]
        if exp["known"]:
            # Known materials must carry documented numeric properties.
            assert limit.properties
            assert all(isinstance(v, float) for v in limit.properties.values())
        else:
            assert limit.properties == {}

    # --- Tolerance ranges: ISO 2768 general tolerance + GD&T zones ------------
    tol_by_source = {t.source: t for t in result.tolerance_ranges}
    if expected["iso_class"] is not None:
        general = tol_by_source["title_block.general_tolerance"]
        assert general.label == f"ISO 2768-{expected['iso_class']}"
        assert general.metadata["class"] == expected["iso_class"]
        assert general.unit == "mm"
        assert general.lower <= general.upper
    else:
        assert "title_block.general_tolerance" not in tol_by_source

    for i, upper in enumerate(expected["gdt_upper"]):
        zone = tol_by_source[f"gdt_callouts[{i}]"]
        # GD&T tolerance zones are total: [0, tolerance_value].
        assert zone.lower == 0.0
        assert zone.upper == pytest.approx(upper)

    expected_tol_count = (1 if expected["iso_class"] is not None else 0) + len(
        expected["gdt_upper"]
    )
    assert len(result.tolerance_ranges) == expected_tol_count

    # --- Present / missing categories -----------------------------------------
    assert set(result.present_categories) == set(expected["present"])
    assert set(result.missing_categories) == set(expected["missing"])

    payload = result.to_dict()
    assert payload["can_optimize"] == (len(expected["missing"]) < len(cv.ALL_CATEGORIES))


# ===========================================================================
# Property 7: NSGA-II Produces Valid Pareto Results
# ===========================================================================

# Fields the Bar-Cohen model exposes plus design-space params. Any field
# resolves to a numeric objective value (unknown fields resolve to 0.0).
_METRIC_FIELDS = [
    "heat_dissipation_W",
    "weight_kg",
    "volume_m3",
    "thermal_resistance_KpW",
    "surface_temperature_C",
]
_PARAM_NAMES = [
    "fin_count",
    "fin_height_m",
    "width_m",
    "length_m",
    "fin_thickness_m",
    "base_thickness_m",
]
_OBJECTIVE_FIELDS = _METRIC_FIELDS + _PARAM_NAMES
_CONSTRAINT_TYPES = ["max", "min", "eq"]

# Small budgets/populations keep each NSGA-II run fast (Property 7 asserts
# behaviour, not scale). Budgets remain within the required [100, 10000] range.
_budget = st.sampled_from([100, 120, 150, 180, 200])
_population = st.integers(min_value=10, max_value=20)


@st.composite
def _design_space(draw) -> dict:
    """Draw 1-3 continuous/integer design variables with valid [min, max] bounds."""
    names = draw(
        st.lists(st.sampled_from(_PARAM_NAMES), min_size=1, max_size=3, unique=True)
    )
    space: dict[str, dict] = {}
    for name in names:
        lo = draw(st.floats(min_value=0.001, max_value=0.5, allow_nan=False))
        span = draw(st.floats(min_value=0.001, max_value=0.5, allow_nan=False))
        var_type = draw(st.sampled_from(["float", "int"]))
        if var_type == "int":
            # Integer variables use a small positive count range.
            lo_i = draw(st.integers(min_value=1, max_value=20))
            hi_i = lo_i + draw(st.integers(min_value=1, max_value=20))
            space[name] = {"min": lo_i, "max": hi_i, "type": "int"}
        else:
            space[name] = {"min": lo, "max": lo + span, "type": "float"}
    return space


@st.composite
def _objectives(draw) -> list[str]:
    """Draw 1-3 objectives with unique fields (so objective keys stay distinct)."""
    fields = draw(
        st.lists(st.sampled_from(_OBJECTIVE_FIELDS), min_size=1, max_size=3, unique=True)
    )
    return [
        f"{draw(st.sampled_from(['maximize', 'minimize']))}:{field}" for field in fields
    ]


@st.composite
def _constraints(draw) -> dict:
    """Draw 0-2 constraints referencing metric/param fields with numeric limits."""
    names = draw(
        st.lists(
            st.sampled_from(["c_temp", "c_weight", "c_volume", "c_resistance"]),
            max_size=2,
            unique=True,
        )
    )
    constraints: dict[str, dict] = {}
    for name in names:
        constraints[name] = {
            "field": draw(st.sampled_from(_METRIC_FIELDS)),
            "type": draw(st.sampled_from(_CONSTRAINT_TYPES)),
            "limit": draw(st.floats(min_value=0.1, max_value=500.0, allow_nan=False)),
        }
    return constraints


@pytest.fixture(autouse=True)
def _clean_optimizer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a clean, local-only optimizer configuration for every example."""
    for var in (
        "SAGEMAKER_ENDPOINT_NAME",
        "SAGEMAKER_REGION",
        "SAGEMAKER_TIMEOUT_SECONDS",
        "OPT_DEFAULT_BUDGET",
        "OPT_DEFAULT_POPULATION_SIZE",
        "OPT_TIME_BUDGET_SECONDS",
        "OPT_MAX_PARETO",
    ):
        monkeypatch.delenv(var, raising=False)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    objectives=_objectives(),
    design_space=_design_space(),
    constraints=_constraints(),
    budget=_budget,
    population_size=_population,
)
def test_property_7_nsga2_produces_valid_pareto_results(
    objectives: list[str],
    design_space: dict,
    constraints: dict,
    budget: int,
    population_size: int,
) -> None:
    """NSGA-II returns a valid Pareto set for any valid optimization config.

    **Validates: Requirements 5.2, 5.4**
    """
    result = opt.run_optimization(
        objectives=objectives,
        design_space=design_space,
        constraints=constraints,
        budget=budget,
        population_size=population_size,
        model_type="thermal_analytical",
        source_drawing_id="drawing-x",
        extraction_id="extract-x",
    )

    clamped = opt.clamp_budget(budget)
    # Budget in [100, 10000] is unchanged by clamping.
    assert result.budget == clamped == budget

    # Total evaluations never exceed the configured budget (Requirement 5.2).
    assert result.n_evaluations <= result.budget
    assert result.generations * result.population_size <= result.budget

    # At most 20 Pareto designs, at least one, with aligned result lists.
    assert 1 <= len(result.pareto_designs) <= opt.MAX_PARETO_DESIGNS
    assert (
        len(result.pareto_objectives)
        == len(result.pareto_constraints)
        == len(result.pareto_designs)
    )

    expected_obj_keys = set(objectives)
    expected_constraint_names = set(constraints)

    for obj_values in result.pareto_objectives:
        # Every objective has a numeric (finite) value (Requirement 5.4).
        assert set(obj_values) == expected_obj_keys
        for value in obj_values.values():
            assert isinstance(value, float)
            assert math.isfinite(value)

    for statuses in result.pareto_constraints:
        # Pass/fail status per constraint (Requirement 5.4).
        assert set(statuses) == expected_constraint_names
        assert all(isinstance(v, bool) for v in statuses.values())

    # Traceability identifiers are preserved on the result.
    assert result.source_drawing_id == "drawing-x"
    assert result.extraction_id == "extract-x"
