"""Compile compact, offline YAML scenarios into validated Query fixture contracts."""

import argparse
import json
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import Field

from evals.stopping_cases import Action, StoppingCorpus, Text
from query.evidence import EvidenceContract, passage_ranges, text_identity
from query.investigation import Brief, Contract

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = FIXTURES / "stopping_decisions.yaml"
GENERATED = FIXTURES / "stopping_decisions.json"


class SourceText(EvidenceContract):
    text: str = Field(min_length=1)
    reporting_origin: Text


class FixtureSource(Contract):
    format_version: int = Field(default=1, ge=1, le=1)
    design_url: Text
    rubric_definition: dict
    briefs: dict[str, Brief]
    sources: dict[str, SourceText]
    attempts: dict[str, dict]
    actions: dict[str, Action]
    defaults: dict
    expected_defaults: dict
    cases: dict[str, dict] = Field(min_length=1)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject overwritten keys, including accidental duplicate case names."""


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"YAML mapping keys must be unique strings: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_source(path: Path = SOURCE) -> FixtureSource:
    context = yaml.load(path.with_name("stopping_context.yaml").read_text(), Loader=UniqueKeyLoader)
    scenarios = yaml.load(path.read_text(), Loader=UniqueKeyLoader)
    if not isinstance(context, dict) or not isinstance(scenarios, dict):
        raise ValueError("Fixture YAML files must contain mappings")
    if context.keys() & scenarios.keys():
        raise ValueError("Scenario and context files must not redefine the same top-level fields")
    return FixtureSource.model_validate({**context, **scenarios})


def identity(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://fixtures.invalid/stopping/{name}"))


def resolve_cases(source: FixtureSource) -> dict[str, dict]:
    resolved = {}
    for name, changes in source.cases.items():
        expected = changes.get("expected")
        if (
            not isinstance(expected, dict)
            or not {"decision", "target", "answer_integrity", "research_sufficiency", "rationale"}
            <= expected.keys()
        ):
            raise ValueError(f"{name}: every case must declare its own expected decision")
        parent = changes.get("extends")
        if parent is not None:
            if (
                not isinstance(parent, str)
                or parent not in source.cases
                or "extends" in source.cases[parent]
            ):
                raise ValueError(f"{name}: extends must name a base case, not another variant")
            base = source.cases[parent]
        else:
            base = {}
        # Overrides replace whole fields; no recursive merging or implicit list appends.
        case = {**source.defaults, **base, **changes}
        generated = {"id", "evaluation_id", "snapshot", "reporting_origins", "boundary_of"}
        if generated & case.keys():
            raise ValueError(f"{name}: generated fields must not be authored in YAML")
        case.pop("extends", None)
        case["boundary_of"] = parent
        resolved[name] = case
    return resolved


def snapshot(case_id: str, brief_version: int, sources: dict[str, SourceText]) -> dict:
    versions, observations, candidates = [], [], []
    investigation_id = identity("investigation")
    for index, (name, source) in enumerate(sources.items(), 2):
        content = text_identity(source.text).model_dump()
        version_id = identity(f"source/{name}/{content['content_hash']}")
        passages = [
            {
                "id": identity(f"passage/{version_id}/{start}/{end}"),
                "version_id": version_id,
                "start": start,
                "end": end,
            }
            for start, end in passage_ranges(source.text)
        ]
        versions.append(
            {
                "id": version_id,
                "source_id": identity(f"source/{name}"),
                "document_key": name,
                "title": f"Synthetic {name}",
                "origin_url": f"https://fixtures.invalid/{name}",
                "text": source.text,
                **content,
                "passages": passages,
            }
        )
        observations.append(
            {
                "investigation_id": investigation_id,
                "operation_key": f"supply-{name}",
                "brief_version": brief_version,
                "revision": index,
                "document_key": name,
                "title": f"Synthetic {name}",
                "origin_url": f"https://fixtures.invalid/{name}",
                "observed_at": "2026-10-01T12:00:00Z",
                "published_at": None,
                **content,
                "max_bytes": 1048576,
                "status": "accepted",
                "version_id": version_id,
                "reason": None,
            }
        )
        candidates.append(
            {
                "version_id": version_id,
                "observation_keys": [f"supply-{name}"],
                "passages": [
                    {
                        "passage_id": p["id"],
                        "start": p["start"],
                        "end": p["end"],
                        "included": True,
                        "reason": None,
                    }
                    for p in passages
                ],
            }
        )
    size = sum(v["byte_count"] for v in versions)
    if size > 262144:
        raise ValueError("Prepared stopping evidence exceeds the snapshot byte allowance")
    snapshot_key = json.dumps([case_id, brief_version, [v["id"] for v in versions]])
    return {
        "snapshot": {
            "id": identity(f"snapshot/{snapshot_key}"),
            "investigation_id": investigation_id,
            "operation_key": "freeze-1",
            "brief_version": brief_version,
            "revision": len(versions) + 2,
            "format_version": 1,
            "status": "ready",
            "reason": None,
            "candidates": candidates,
            "rejected_inputs": [],
            "requested_bytes": size,
            "included_bytes": size,
            "max_bytes": 262144,
        },
        "versions": versions,
        "observations": observations,
    }


def compile_corpus(source: FixtureSource) -> StoppingCorpus:
    cases = []
    for name, resolved in resolve_cases(source).items():
        case = dict(resolved)
        try:
            case["brief"] = source.briefs[case["brief"]].model_dump(mode="json")
            source_keys = case.pop("sources")
            if not isinstance(source_keys, list) or any(
                not isinstance(key, str) for key in source_keys
            ):
                raise ValueError(f"{name}: sources must be a list of names")
            if len(source_keys) != len(set(source_keys)):
                raise ValueError(f"{name}: duplicate source reference")
            selected_sources = {key: source.sources[key] for key in source_keys}
            evidence = snapshot(name, case["checkpoint"]["brief_version"], selected_sources)
            versions = {v["document_key"]: v["id"] for v in evidence["versions"]}
            attempts = []
            for key in case["attempts"]:
                attempt = dict(source.attempts[key])
                attempt["version_ids"] = [versions[s] for s in attempt.pop("sources")]
                attempts.append(attempt)
            case["attempts"] = attempts
            questions = []
            for question in case["questions"]:
                question = dict(question)
                question["version_ids"] = [versions[s] for s in question.pop("sources")]
                question["attempt_ids"] = [
                    source.attempts[a]["id"] for a in question.pop("attempts")
                ]
                questions.append(question)
            case["questions"] = questions
            case["actions"] = [source.actions[key].model_dump() for key in case["actions"]]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"{name}: unknown or missing fixture reference {exc}") from exc
        cases.append(
            {
                **case,
                "id": name,
                "evaluation_id": identity(f"evaluation/{name}"),
                "snapshot": evidence,
                "reporting_origins": {
                    versions[k]: s.reporting_origin for k, s in selected_sources.items()
                },
                "expected": {**source.expected_defaults, **case["expected"]},
            }
        )
    return StoppingCorpus.model_validate(
        {
            "format_version": source.format_version,
            "design_url": source.design_url,
            "rubric_version": source.rubric_definition["version"],
            "rubric_definition": source.rubric_definition,
            "cases": cases,
        }
    )


def render_json(corpus: StoppingCorpus) -> str:
    return corpus.model_dump_json(indent=2) + "\n"


def review_case(source: FixtureSource, name: str) -> dict:
    """Resolve a scenario for reading without UUIDs, hashes, or snapshot boilerplate."""
    case = dict(resolve_cases(source)[name])
    case["brief"] = source.briefs[case["brief"]].model_dump(mode="json")
    case["sources"] = {key: source.sources[key].model_dump() for key in case["sources"]}
    case["attempts"] = [source.attempts[key] for key in case["attempts"]]
    case["actions"] = [source.actions[key].model_dump(mode="json") for key in case["actions"]]
    case["expected"] = {**source.expected_defaults, **case["expected"]}
    return {"id": name, **case}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["generate", "check", "review"])
    parser.add_argument("--case", help="Scenario name for a resolved review view")
    args = parser.parse_args(argv)
    try:
        source = load_source()
        corpus = compile_corpus(source)
        if args.command == "review":
            names = [args.case] if args.case else source.cases
            print(yaml.safe_dump([review_case(source, n) for n in names], sort_keys=False))
        elif args.command == "generate":
            GENERATED.write_text(render_json(corpus))
        elif GENERATED.read_text() != render_json(corpus):
            raise ValueError("Generated JSON is stale; run mise run fixtures:stopping")
        return 0
    except (ValueError, KeyError, OSError, yaml.YAMLError) as exc:
        print(f"generate_stopping: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
