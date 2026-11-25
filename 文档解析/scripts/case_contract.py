"""Normalize the case's output contract and validate deliverable JSON offline."""
from __future__ import annotations

import re


STATUS_NAMES = {
    "success": ("success", "ok", "complete", "completed", "succeeded", "成功", "完成"),
    "partial": ("partial", "partially_completed", "partial_completion", "partially_complete", "部分完成", "部分成功"),
    "indeterminate": (
        "indeterminate", "cannot_determine", "unable_to_determine", "undetermined",
        "uncertain", "inconclusive", "insufficient_evidence", "unanswerable", "unknown",
        "failed", "error", "无法判断", "证据不足", "无法完成",
    ),
    "refused": ("refused", "refusal", "refuse", "declined", "rejected", "拒答", "拒绝"),
}
STATUS_REASON_FIELDS = (
    "status_reason", "reason", "failure_reason", "limitations", "uncertainty", "uncertainties", "notes",
)
ALIASES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
           "dict": "object", "list": "array"}
VALUE_TYPES = {
    "string": (str,), "integer": (int,), "number": (int, float),
    "boolean": (bool,), "object": (dict,), "array": (list,), "null": (type(None),),
}
ANNOTATIONS = {"$schema", "$id", "title", "description", "default", "examples",
               "deprecated", "readOnly", "writeOnly", "$comment", "format"}
LOCAL_RULES = {
    "type", "properties", "required", "items", "additionalProperties", "enum", "const",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "minimum", "maximum",
}
JSON_SCHEMA_MARKERS = {
    "type", "properties", "required", "additionalProperties", "$ref", "$schema",
    "$defs", "definitions", "allOf", "anyOf", "oneOf", "not", "if",
}
SCHEMA_ENVELOPES = ("json_schema", "schema", "result_schema", "result_json_schema", "result_json", "result.json")


def status_kind(value):
    if isinstance(value, dict):
        value = value.get("code", value.get("status"))
    if isinstance(value, str):
        key = re.sub(r"[\s-]+", "_", value.strip().lower())
        return next((kind for kind, names in STATUS_NAMES.items() if key in names), None)
    return None


def field_schema(value):
    if isinstance(value, str):
        name = ALIASES.get(value, value)
        return {"type": name} if name in VALUE_TYPES else {"description": value}
    if isinstance(value, dict):
        result = {k: v for k, v in value.items() if k not in {"name", "field", "key", "required"}}
        if isinstance(value.get("required"), list):
            result["required"] = value["required"]
        if isinstance(result.get("type"), str):
            result["type"] = ALIASES.get(result["type"], result["type"])
        return result
    return {}


def result_schema(contract):
    """Return a structured schema, or None for prose requiring agent judgment."""
    if isinstance(contract, list):
        contract = {"fields": contract}
    if not isinstance(contract, dict):
        return None
    for member in SCHEMA_ENVELOPES:
        if isinstance(contract.get(member), (dict, list)):
            child = result_schema(contract[member])
            if child is not None:
                return child
    typed_schema = contract.get("type") == "object" or isinstance(contract.get("type"), list)
    if typed_schema or any(key in contract for key in JSON_SCHEMA_MARKERS - {"required", "type"}):
        return {k: v for k, v in contract.items()
                if k not in {"required_files", "artifacts", "required_artifacts"}}

    fields = contract.get("fields", contract.get("required_fields", contract.get("required")))
    if not isinstance(fields, (list, dict)):
        return None
    properties, required = {}, []
    for entry in fields if isinstance(fields, list) else fields.items():
        if isinstance(entry, str):
            name, rules = entry, {}
        elif isinstance(entry, tuple):
            name, rules = entry
        elif isinstance(entry, dict):
            name, rules = entry.get("name", entry.get("field", entry.get("key"))), entry
        else:
            raise ValueError("Invalid output field descriptor")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Output field descriptor needs a name")
        properties[name] = field_schema(rules)
        if not isinstance(rules, dict) or rules.get("required") is not False:
            required.append(name)
    explicit = contract.get("required_fields", contract.get("required"))
    if isinstance(explicit, list) and all(isinstance(name, str) for name in explicit):
        required = explicit
    return {"type": "object", "properties": properties, "required": list(dict.fromkeys(required))}


def required_artifacts(contract):
    if not isinstance(contract, dict):
        return []
    items = []
    for key in ("required_files", "required_artifacts", "artifacts"):
        if isinstance(contract.get(key), list):
            items.extend(contract[key])
    paths = []
    for item in items:
        if isinstance(item, dict):
            if item.get("required") is False:
                continue
            item = item.get("path", item.get("filename", item.get("file")))
        if isinstance(item, str) and item and item.rsplit("/", 1)[-1] != "result.json":
            paths.append(item)
    return list(dict.fromkeys(paths))


def with_case_id(schema):
    """The runtime requires case_id in addition to a closed result payload."""
    if isinstance(schema, dict) and schema.get("additionalProperties") is False:
        properties = schema.get("properties", {})
        if isinstance(properties, dict) and "case_id" not in properties:
            return {**schema, "properties": {**properties, "case_id": {}}}
    return schema


def local_errors(value, schema, at="$"):
    if isinstance(schema, bool):
        return [] if schema else [f"{at} forbidden by schema"]
    if not isinstance(schema, dict):
        return [f"{at} invalid schema"]
    unknown = sorted(schema.keys() - LOCAL_RULES - ANNOTATIONS)
    if unknown:
        return [f"{at} needs jsonschema support for: {', '.join(unknown)}"]
    expected = schema.get("type")
    if isinstance(expected, str):
        expected = [expected]
    if expected is not None:
        if not isinstance(expected, list) or any(k not in VALUE_TYPES for k in expected):
            return [f"{at} invalid type in schema"]
        whole_number = type(value) is float and value.is_integer()
        if not any(type(value) in VALUE_TYPES[k] or k == "integer" and whole_number for k in expected):
            return [f"{at} must be {' or '.join(expected)}"]
    if ("const" in schema and not json_equal(value, schema["const"])) or (
            "enum" in schema and not any(json_equal(value, item) for item in schema["enum"])):
        return [f"{at} does not match the contract's allowed values"]

    errors = []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{at}.{key} is required")
        extra = schema.get("additionalProperties", True)
        for key, member in value.items():
            errors.extend(local_errors(member, props.get(key, extra), f"{at}.{key}"))
    if isinstance(value, list):
        rule = schema.get("items", True)
        for index, member in enumerate(value):
            errors.extend(local_errors(member, rule, f"{at}[{index}]"))
            if len(errors) >= 20:
                break
    if isinstance(value, (str, list)):
        label = "Length" if isinstance(value, str) else "Items"
        if len(value) < schema.get("min" + label, 0):
            errors.append(f"{at} is shorter than allowed")
        if len(value) > schema.get("max" + label, len(value)):
            errors.append(f"{at} is longer than allowed")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{at} does not match the required pattern")
    if type(value) in (int, float):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            errors.append(f"{at} is outside the allowed range")
    return errors[:20]


def json_equal(left, right):
    # In JSON, booleans and numbers are distinct.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    return left == right


def schema_errors(data, schema):
    if schema is None:
        return [], "agent_review"
    try:
        from jsonschema.validators import validator_for
        from referencing import Registry
    except ImportError:
        try:
            return local_errors(data, schema), "local"
        except (TypeError, ValueError, AttributeError, re.error) as err:
            return [f"Invalid output contract: {err}"], "local"
    try:
        validator_type = validator_for(schema)
        validator_type.check_schema(schema)
        validator = validator_type(schema, registry=Registry())
        problems = []
        for err in validator.iter_errors(data):
            if err.validator == "required":
                details = ", ".join(key for key in err.validator_value if key not in err.instance)
                problem = f"missing required fields: {details}"
            else:
                problem = f"constraint failed: {err.validator}"
            problems.append(f"{err.json_path}: {problem}")
            if len(problems) == 20:
                break
        return list(dict.fromkeys(problems)), "jsonschema"
    except Exception as err:
        return [f"Unable to validate the contract: {type(err).__name__}: {str(err)[:280]}"], "jsonschema"


def enforce_status(data, schema):
    value = data.get("status")
    node = value.get("code", value.get("status")) if isinstance(value, dict) else value
    kind = status_kind(value)
    rules = field_rules(schema, "status")
    if isinstance(value, dict):
        rules = field_rules(rules, "code" if "code" in value else "status")
    defined = isinstance(node, str) and (node in rules.get("enum", []) or node == rules.get("const"))
    if not kind and not defined:
        return ["$.status must be a completion state or a state explicitly defined by the contract"]
    if kind == "success":
        return []
    reasons = [data.get(key) for key in STATUS_REASON_FIELDS]
    if isinstance(value, dict):
        reasons.append(value.get("reason"))
    if any(has_reason(why) for why in reasons):
        return []
    return ["Add an actual limitation or reason for the non-success status"]


def has_reason(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(has_reason(member) for member in value.values())
    if isinstance(value, list):
        return any(has_reason(member) for member in value)
    return False


def field_rules(schema, name):
    if not isinstance(schema, dict):
        return {}
    rule = schema.get("properties", {}).get(name, {})
    return rule if isinstance(rule, dict) else {}


def allows_field(schema, name):
    if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
        return True
    return name in schema.get("properties", {})


def failure_status(schema, kind, reason):
    rule = field_rules(schema, "status")
    use_object = rule.get("type") == "object"
    key = "status" if "status" in rule.get("properties", {}) else "code"
    code_rule = field_rules(rule, key) if use_object else rule
    allowed = code_rule.get("enum", [code_rule["const"]] if "const" in code_rule else [])
    code = next((value for value in allowed if status_kind(value) == kind), kind)
    if not use_object:
        return code
    result = {key: code}
    if allows_field(rule, "reason"):
        result["reason"] = reason
    return result


def empty_value(rule):
    """Use explicit unknowns for unanswered fields, with no synthetic values."""
    types = rule.get("type")
    if types == "array" and not rule.get("minItems"):
        return []
    if types == "object":
        return {name: empty_value(field_rules(rule, name)) for name in rule.get("required", [])}
    return None


def incomplete_result(case_id, schema, reason, kind="indeterminate"):
    required = schema.get("required", []) if isinstance(schema, dict) else []
    data = {name: empty_value(field_rules(schema, name)) for name in required}
    data.update(case_id=case_id, status=failure_status(schema, kind, reason))
    for name in STATUS_REASON_FIELDS:
        if not allows_field(schema, name):
            continue
        rule = field_rules(schema, name)
        if rule.get("type") == "object":
            continue
        data[name] = [reason] if rule.get("type") == "array" else reason
        break
    for name in ("evidence", "uncertainties", "changes"):
        if name not in data and allows_field(schema, name):
            rule = field_rules(schema, name)
            if rule.get("type", "array") == "array":
                data[name] = [reason] if name == "uncertainties" else []
    return data
