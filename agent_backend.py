"""One Chat Completions backend; E1 sends no model tools."""

import asyncio
from copy import deepcopy
import time

from openai import AsyncOpenAI, APIConnectionError, APIError, APIStatusError, APITimeoutError


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


class ChatBackend:
    def __init__(self, config, api_key, base_url, http_client=None):
        self.config = config
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                                  timeout=config["timeout_seconds"], http_client=http_client)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.close()

    async def generate(self, messages, trace):
        request = {"model": self.config["model"], "messages": messages,
                   "temperature": self.config["temperature"],
                   "max_tokens": self.config["max_output_tokens"]}
        # Only connection failures get one retry, within the same candidate attempt.
        for retry in range(2):
            event = {"retry": retry, "request": deepcopy(request)}
            trace.append(event)
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(self.client.chat.completions.create(**request),
                                                  self.config["timeout_seconds"])
                event["response"] = response.model_dump(mode="json", exclude_none=True)
                if len(response.choices) != 1:
                    raise BackendError("model_protocol_error: expected exactly one choice")
                choice = response.choices[0]
                return {"finish_reason": choice.finish_reason,
                        "message": choice.message.model_dump(mode="json", exclude_none=True)}
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
