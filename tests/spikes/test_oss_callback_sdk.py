"""Run with the pinned LiteLLM image; no license, credentials or network."""

import asyncio
import importlib.metadata
import json

import litellm

from oss_callback_probe import INPUT_MARKER, OUTPUT_MARKER, ProbeLogger


async def main():
    assert importlib.metadata.version("litellm") == "1.98.0"
    logger = ProbeLogger()
    litellm.callbacks = [logger]
    litellm.telemetry = False
    response = await litellm.acompletion(
        model="openai/synthetic-model",
        messages=[{"role": "user", "content": INPUT_MARKER}],
        mock_response=OUTPUT_MARKER,
    )
    assert response.choices[0].message.content == OUTPUT_MARKER
    await asyncio.wait_for(logger.terminal.wait(), timeout=10)
    event = next(item for item in logger.events if item["event"] == "success")
    assert event["input_present"] and event["output_present"]
    assert event["call_id_present"]
    print(json.dumps({"version": "1.98.0", "sdk_custom_logger": "passed", "evidence": event}))


if __name__ == "__main__":
    asyncio.run(main())