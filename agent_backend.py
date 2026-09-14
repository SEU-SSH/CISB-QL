"""One stateless Responses backend with optional bounded tools supplied by the harness."""

import asyncio
from copy import deepcopy
import time

from openai import AsyncOpenAI, APIConnectionError, APIError, APIStatusError, APITimeoutError


API_FORMAT = "responses"


class BackendError(Exception):
    pass


def redact(value, sensitive):
    if isinstance(value, str):
        for secret in sensitive:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, sensitive) for item in value]
    if isinstance(value, dict):
        return {redact(key, sensitive): redact(item, sensitive) for key, item in value.items()}
    return value


def validate_response(response):
    if response.get("object") != "response" or not isinstance(response.get("output"), list):
        raise BackendError("model_protocol_error: expected a Responses output list")
    if response.get("error") or response.get("status") not in ("completed", "incomplete"):
        raise BackendError(f"model_response_error: status={response.get('status')}; error={response.get('error')}")


def response_text(response):
    validate_response(response)
    if response["status"] != "completed":
        details = response.get("incomplete_details") or {}
        raise ValueError(f"incomplete response: {details.get('reason', 'unknown')}")
    parts = []
    for item in response["output"]:
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ValueError("expected final text, without tool calls or other output items")
        if item.get("status") != "completed":
            raise ValueError("incomplete output message")
        for content in item.get("content", []):
            if content.get("type") != "output_text" or not isinstance(content.get("text"), str):
                raise ValueError("expected output_text, without refusal or other content")
            parts.append(content["text"])
    if not parts or not "".join(parts).strip():
        raise ValueError("missing response text")
    return "".join(parts)


class ResponsesBackend:
    def __init__(self, config, api_key, base_url, http_client=None):
        self.config = config
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                                  timeout=config["timeout_seconds"], http_client=http_client)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.close()

    async def generate(self, instructions, input_items, trace, *, tools=None, tool_choice="auto"):
        request = {"model": self.config["model"], "instructions": instructions, "input": input_items,
                   "temperature": self.config["temperature"],
                   "max_output_tokens": self.config["max_output_tokens"]}
        if tools is not None:
            request.update(tools=tools, tool_choice=tool_choice, parallel_tool_calls=False)
        # Only connection failures get one retry, within the same candidate attempt.
        for retry in range(2):
            event = {"retry": retry, "request": deepcopy(request)}
            trace.append(event)
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(self.client.responses.create(**request),
                                                  self.config["timeout_seconds"])
                event["response"] = response.model_dump(mode="json", exclude_none=True)
                validate_response(event["response"])
                return event["response"]
            except BackendError as error:
                event["error"] = {"type": type(error).__name__, "message": str(error)}
                raise
            except (TimeoutError, APITimeoutError) as error:
                event["error"] = {"type": type(error).__name__, "message": str(error)}
                raise BackendError("model_timeout: no code repair attempted") from error
            except APIConnectionError as error:
                event["error"] = {"type": type(error).__name__, "message": str(error)}
                if retry == 1:
                    raise BackendError(f"model_transport_error: {error}") from error
            except APIStatusError as error:
                event["error"] = {"type": type(error).__name__, "message": str(error),
                                  "status_code": error.status_code}
                raise BackendError(f"model_api_error ({error.status_code}): {error}") from error
            except APIError as error:
                event["error"] = {"type": type(error).__name__, "message": str(error)}
                raise BackendError(f"model_protocol_error: {error}") from error
            except asyncio.CancelledError:
                event["error"] = {"type": "CancelledError", "message": "request cancelled"}
                raise
            finally:
                event["seconds"] = time.monotonic() - started
