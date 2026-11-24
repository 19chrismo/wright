"""Documented formats: every on-disk artifact validates against its JSON Schema in schemas/."""
import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

from wright.baseline import Baseline, Dimension
from wright.calibration import CalibrationTable
from wright.config import PACKAGE_DIR, Paths
from wright.contract import RoleContract
from wright.demos import step3
from wright.memory import EpisodicLog
from wright.skills import Skill, SkillLibrary

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
REG = Registry().with_resources(
    [(p.name, Resource.from_contents(json.loads(p.read_text()))) for p in SCHEMAS.glob("*.json")])


def validate(name, obj):
    schema = json.loads((SCHEMAS / name).read_text())
    jsonschema.Draft202012Validator(schema, registry=REG).validate(json.loads(json.dumps(obj, default=str)))


def test_schemas_are_valid_json_schema():
    for p in SCHEMAS.glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(json.loads(p.read_text()))


def test_knowledge_pack():
    validate("knowledge-pack.schema.json", step3(verbose=False)["pack"].as_dict())


def test_baseline_manifest(tmp_path):
    b = Baseline("bl", str(tmp_path), [Dimension("t", {"kind": "command", "run": "true"}),
                                       Dimension("ms", {"kind": "metric", "run": "echo 5"}, "lower", 0.1)])
    b.freeze(tmp_path)
    b.save(tmp_path / "b.json")
    validate("baseline.schema.json", json.loads((tmp_path / "b.json").read_text()))
    with pytest.raises(jsonschema.ValidationError):
        validate("baseline.schema.json", {**b.as_dict(), "dimensions": [{"name": "x", "check": {"kind": "command"},
                                                                         "direction": "pass"}]})


def test_role_contract_and_plan_graph():
    validate("role-contract.schema.json", RoleContract().to_dict())
    validate("plan-graph.schema.json", json.loads((PACKAGE_DIR / "plans" / "default.json").read_text()))


def test_episodic_entries_and_calibration_table(tmp_path):
    from wright.session import Continuity
    from test_step08_calibration import seed
    paths = Paths(tmp_path / "h")
    c = Continuity(paths)
    c.open()
    seed(c.log, "net", "shell__run", "harden", ["held", "wrong"])
    from wright.demos import step1
    t = step1(tmp_path / "w", verbose=False)
    c.close_task(t)
    for row in EpisodicLog(paths.episodic).all():
        validate("episodic-entry.schema.json", row)
    validate("calibration.schema.json", json.loads(paths.calibration_file.read_text()))


def test_skill_definition_with_regression_test(tmp_path):
    lib = SkillLibrary(tmp_path)
    lib.save(Skill(name="touch_file", description="make a file", steps=[{"tool": "shell__run",
             "args": {"command": "touch {{name}}"}}], params={"name": {"type": "string"}},
                   test={"params": {"name": "x"}, "checks": [{"kind": "file_exists", "path": "x"}]}), "created")
    validate("skill.schema.json", json.loads((tmp_path / "touch_file" / "v1.json").read_text()))
