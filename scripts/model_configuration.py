"""Safe per-model configuration subset supported by pinned LiteLLM 1.104."""

import copy
import math
import re
from urllib.parse import urlsplit

from scripts.customer_migration import MigrationError, require


COST_FIELDS = frozenset({
    "input_cost_per_token", "output_cost_per_token",
    "cache_creation_input_token_cost", "cache_read_input_token_cost",
    "input_cost_per_token_above_128k_tokens", "output_cost_per_token_above_128k_tokens",
    "input_cost_per_token_above_200k_tokens", "output_cost_per_token_above_200k_tokens",
    "cache_creation_input_token_cost_above_200k_tokens", "cache_read_input_token_cost_above_200k_tokens",
    "input_cost_per_image", "input_cost_per_image_token",
    "input_cost_per_audio_token", "output_cost_per_audio_token", "output_cost_per_reasoning_token",
    "input_cost_per_character", "output_cost_per_character",
    "input_cost_per_audio_per_second", "input_cost_per_video_per_second",
    "input_cost_per_video_per_second_above_128k_tokens", "input_cost_per_second", "output_cost_per_second",
    "input_cost_per_token_priority", "output_cost_per_token_priority",
    "input_cost_per_token_flex", "output_cost_per_token_flex",
    "input_cost_per_token_batches", "output_cost_per_token_batches",
})
POSITIVE_INTS = frozenset({"max_tokens", "max_completion_tokens", "n", "rpm", "tpm", "dimensions"})
PARAM_FIELDS = POSITIVE_INTS | {
    "api_version", "seed", "top_logprobs", "num_retries", "temperature", "top_p",
    "presence_penalty", "frequency_penalty", "timeout", "stream", "logprobs", "stop",
    "reasoning_effort", "encoding_format", "response_format",
}
INFO_FIELDS = COST_FIELDS | {"base_model", "mode", "max_tokens", "max_input_tokens", "max_output_tokens"}
IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(IDENTIFIER, value) is not None,
            "Model identifiers/API versions must be plain non-empty identifiers")
    return value


def finite_number(value, minimum=0, maximum=None):
    require(type(value) in {int, float}, "Model numbers must be numeric literals, not booleans, strings or null")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    require(finite and value >= minimum and (maximum is None or value <= maximum),
            "Model number must be finite and within the supported range")


def integer(value, minimum=1, maximum=2**31 - 1):
    require(type(value) is int and minimum <= value <= maximum,
            "Model integer must be an integer literal within the supported range")


def azure_base_model(value):
    require(isinstance(value, str), "base_model must be a plain Azure model-family identifier")
    name = value.removeprefix("azure/")
    identifier(name)
    return "azure/" + name


def validate_options(params, info, *, catalog=False):
    require(isinstance(params, dict) and isinstance(info, dict), "litellm_params/model_info must be objects")
    allowed_params = PARAM_FIELDS if catalog else PARAM_FIELDS - {"api_version"}
    allowed_info = INFO_FIELDS if catalog else INFO_FIELDS - {"base_model"}
    require("cost_per_second" not in info and "cost_per_second" not in params,
            "cost_per_second requires LiteLLM >=1.105; this gateway pins 1.104")
    require(not params.keys() - allowed_params and not info.keys() - allowed_info,
            "Unsupported/reserved model fields; credentials, provider/endpoints, headers, TLS, IDs and global overrides are forbidden")
    require(all(value is not None for value in [*params.values(), *info.values()]),
            "Model options cannot be null; omission preserves existing values, implicit clearing is unsupported")
    for field, value in params.items():
        if field in POSITIVE_INTS:
            integer(value)
        elif field == "api_version":
            identifier(value)
        elif field == "seed":
            integer(value, -(2**63), 2**63 - 1)
        elif field == "top_logprobs":
            integer(value, 0, 20)
        elif field == "num_retries":
            integer(value, 0)
        elif field in {"temperature", "top_p", "presence_penalty", "frequency_penalty"}:
            lower, upper = {"temperature": (0, 2), "top_p": (0, 1),
                            "presence_penalty": (-2, 2), "frequency_penalty": (-2, 2)}[field]
            finite_number(value, lower, upper)
        elif field == "timeout":
            finite_number(value)
            require(value > 0, "timeout must be positive")
        elif field in {"stream", "logprobs"}:
            require(type(value) is bool, field + " must be a boolean")
        elif field == "stop":
            values = [value] if isinstance(value, str) else value
            require(isinstance(values, list) and 1 <= len(values) <= 4
                    and all(isinstance(item, str) and 1 <= len(item) <= 1024 for item in values),
                    "stop must be a nonempty string or one to four nonempty strings (max 1024 characters each)")
        elif field == "reasoning_effort":
            require(isinstance(value, str) and value in {"low", "medium", "high"},
                    "reasoning_effort must be low, medium or high")
        elif field == "encoding_format":
            require(isinstance(value, str) and value in {"float", "base64"}, "encoding_format must be float or base64")
        elif field == "response_format":
            require(isinstance(value, dict) and set(value) == {"type"}
                    and isinstance(value["type"], str) and value["type"] in {"text", "json_object"},
                    "response_format supports only {type:text|json_object}")
    require(not {"max_tokens", "max_completion_tokens"} <= params.keys(),
            "Specify only one of max_tokens and max_completion_tokens")
    for field, value in info.items():
        if field in COST_FIELDS:
            finite_number(value)
        elif field.startswith("max_"):
            integer(value)
        elif field == "base_model":
            azure_base_model(value)
        elif field == "mode":
            require(isinstance(value, str) and value in {"chat", "embedding", "completion"},
                    "model_info.mode must be chat, embedding or completion")


def normalize_openai_endpoint(endpoint):
    require(isinstance(endpoint, str), "Azure OpenAI endpoint must be a literal HTTPS URL")
    require(endpoint == endpoint.strip() and not any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)
            and "?" not in endpoint and "#" not in endpoint,
            "Azure OpenAI endpoint cannot contain whitespace, control characters, query or fragment")
    try:
        parsed = urlsplit(endpoint)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        raise MigrationError("Azure OpenAI endpoint has an invalid URL or port") from None
    require(parsed.scheme == "https" and isinstance(hostname, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]\.openai\.azure\.com", hostname) is not None
            and parsed.username is None and parsed.password is None and port in {None, 443}
            and parsed.netloc.lower() in {hostname, hostname + ":443"}
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
            "Only plain public-cloud Azure OpenAI HTTPS account roots on port 443 are supported")
    return "https://" + hostname


def connection_endpoint(connection):
    endpoint = connection.get("endpoint", "https://" + connection["accountName"] + ".openai.azure.com")
    return normalize_openai_endpoint(endpoint)


def rendered_models(config):
    accounts = {item["alias"]: item for item in config["parameters"]["platform"]["azureOpenAIConnections"]}
    result = []
    for model in config["application"]["models"]:
        validate_options(model.get("litellmParams", {}), model.get("modelInfo", {}))
        base = {"base_model": model["baseModel"]} if "baseModel" in model else {}
        result.append({"model_name": model["modelGroup"],
                       "litellm_params": {**copy.deepcopy(model.get("litellmParams", {})),
                                         "model": "azure/" + model["deploymentName"],
                                         "api_base": connection_endpoint(accounts[model["connectionAlias"]]),
                                         "api_version": model["apiVersion"], **base},
                       "model_info": {**copy.deepcopy(model.get("modelInfo", {})), "id": model["id"], **base}})
    return result


def pricing_warnings(config):
    return ["Explicit zero input/output token costs bypass LiteLLM budget checks: " + model["id"]
            for model in config["application"]["models"]
            if model.get("modelInfo", {}).get("input_cost_per_token") == 0
            and model.get("modelInfo", {}).get("output_cost_per_token") == 0]
