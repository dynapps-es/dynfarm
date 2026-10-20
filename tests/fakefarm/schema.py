"""A small OpenAPI reader for the fake farm: it routes a request to an operation and validates JSON against schemas.

This is what makes the fake farm more than a stub (D17): the pinned `tests/fixtures/openapi.json` decides which
operations exist, which query parameters and body fields are allowed, which are required, and what shape every answer
must have. A CLI that sends a field the API does not know, or forgets a required one, is told so the way the real farm
would tell it (422) and the fake records it as a violation that fails the test.

It implements only the corner of JSON Schema that this OpenAPI uses: `$ref`, `anyOf`, `type`, `enum`, string length and
pattern, `format: date-time`, number bounds, arrays with `items`/`maxItems`, objects with `required`, `properties` and
`additionalProperties`. A keyword outside that list is ignored, so a new API feature does not break the fake; the
pinned document is refreshed deliberately (`CONTRIBUTING.md`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import unquote

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass
class Route:
    method: str
    template: str
    operation_id: str
    regex: re.Pattern[str]
    parameters: list[dict[str, Any]]
    scopes: list[str]
    body_schema: dict[str, Any] | None
    responses: dict[str, Any]


@dataclass
class Problem:
    """One validation failure, in FastAPI's `detail` item shape."""

    loc: list[str | int]
    msg: str
    type: str

    def as_detail(self) -> dict[str, Any]:
        return {"loc": self.loc, "msg": self.msg, "type": self.type}


@dataclass
class Openapi:
    document: dict[str, Any]
    routes: list[Route] = field(default_factory=list)

    def __post_init__(self) -> None:
        for template, item in self.document["paths"].items():
            pattern = (
                "^"
                + re.sub(r"\{([^}/]+)\}", r"(?P<\1>[^/]+)", re.escape(template).replace(r"\{", "{").replace(r"\}", "}"))
                + "$"
            )
            for method in HTTP_METHODS:
                op = item.get(method)
                if op is None:
                    continue
                body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
                security = op.get("security", self.document.get("security", [{}]))
                self.routes.append(
                    Route(
                        method=method.upper(),
                        template=template,
                        operation_id=op["operationId"],
                        regex=re.compile(pattern),
                        parameters=[self.resolve(p) for p in item.get("parameters", []) + op.get("parameters", [])],
                        scopes=list(security[0].get("BearerAuth", [])) if security else [],
                        body_schema=body,
                        responses=op["responses"],
                    )
                )

    @property
    def operation_ids(self) -> set[str]:
        return {route.operation_id for route in self.routes}

    def match(self, method: str, raw_path: str) -> tuple[Route, dict[str, str]] | None:
        """The route for a request, with its percent-decoded path parameters. `None`: no such operation."""
        for route in self.routes:
            found = route.regex.match(raw_path)
            if found and route.method == method.upper():
                return route, {name: unquote(value) for name, value in found.groupdict().items()}
        return None

    def path_exists(self, raw_path: str) -> bool:
        return any(route.regex.match(raw_path) for route in self.routes)

    # -- schemas ----------------------------------------------------------------------------------------------

    def resolve(self, schema: Any) -> Any:
        while isinstance(schema, dict) and "$ref" in schema:
            node: Any = self.document
            for part in schema["$ref"].removeprefix("#/").split("/"):
                node = node[part]
            schema = node
        return schema

    def validate(self, schema: Any, value: Any, loc: list[str | int]) -> list[Problem]:
        schema = self.resolve(schema)
        if not isinstance(schema, dict):
            return []
        if "anyOf" in schema:
            if any(not self.validate(alt, value, loc) for alt in schema["anyOf"]):
                return []
            return [Problem(loc, "Input should match one of the allowed shapes", "union_invalid")]
        problems: list[Problem] = []
        if "enum" in schema and value not in schema["enum"]:
            return [Problem(loc, f"Input should be one of {schema['enum']}", "enum")]
        kind = schema.get("type")
        if kind is None and ("properties" in schema or "additionalProperties" in schema):
            kind = "object"
        if kind and not _is_type(kind, value):
            return [Problem(loc, f"Input should be a valid {kind}", f"{kind}_type")]
        if isinstance(value, str):
            problems += self._string(schema, value, loc)
        elif kind in ("integer", "number") and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                problems.append(
                    Problem(loc, f"Input should be greater than or equal to {schema['minimum']}", "greater_than_equal")
                )
            if "maximum" in schema and value > schema["maximum"]:
                problems.append(
                    Problem(loc, f"Input should be less than or equal to {schema['maximum']}", "less_than_equal")
                )
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                problems.append(
                    Problem(loc, f"Input should be greater than {schema['exclusiveMinimum']}", "greater_than")
                )
        elif isinstance(value, list):
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                problems.append(Problem(loc, f"List should have at most {schema['maxItems']} items", "too_long"))
            for index, item in enumerate(value):
                problems += self.validate(schema.get("items", {}), item, [*loc, index])
        elif isinstance(value, dict):
            problems += self._object(schema, value, loc)
        return problems

    def _string(self, schema: dict[str, Any], value: str, loc: list[str | int]) -> list[Problem]:
        problems = []
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(
                Problem(loc, f"String should have at least {schema['minLength']} characters", "string_too_short")
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(
                Problem(loc, f"String should have at most {schema['maxLength']} characters", "string_too_long")
            )
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(
                Problem(loc, f"String should match pattern '{schema['pattern']}'", "string_pattern_mismatch")
            )
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(value)
            except ValueError:
                problems.append(Problem(loc, "Input should be a valid datetime", "datetime_parsing"))
        return problems

    def _object(self, schema: dict[str, Any], value: dict[str, Any], loc: list[str | int]) -> list[Problem]:
        problems = []
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append(Problem([*loc, name], "Field required", "missing"))
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                problems += self.validate(properties[name], item, [*loc, name])
            elif extra is False:
                problems.append(Problem([*loc, name], "Extra inputs are not permitted", "extra_forbidden"))
            elif isinstance(extra, dict):
                problems += self.validate(extra, item, [*loc, name])
        return problems

    def coerce_query(self, schema: Any, text: str) -> tuple[Any, str | None]:
        """A query value arrives as text; read it as the parameter's type. Returns (value, error message)."""
        schema = self.resolve(schema)
        if isinstance(schema, dict) and "anyOf" in schema:
            options = [alt for alt in schema["anyOf"] if self.resolve(alt).get("type") != "null"]
            schema = self.resolve(options[0]) if options else {}
        kind = schema.get("type") if isinstance(schema, dict) else None
        if kind == "integer":
            try:
                return int(text), None
            except ValueError:
                return text, "Input should be a valid integer, unable to parse string as an integer"
        if kind == "boolean":
            lowered = text.lower()
            if lowered in ("true", "1", "yes", "on"):
                return True, None
            if lowered in ("false", "0", "no", "off"):
                return False, None
            return text, "Input should be a valid boolean"
        return text, None


def _is_type(kind: str, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "null":
        return value is None
    return True
