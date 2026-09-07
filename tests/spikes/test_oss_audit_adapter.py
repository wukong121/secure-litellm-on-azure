import traceback
import unittest

from LiteLLM.observability.oss_audit_callback import OSSAuditCallback


class OSSAuditCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unselected_request_is_not_emitted(self):
        emitted = []
        async def emit(value):
            emitted.append(value)
        callback = OSSAuditCallback(lambda _kwargs: False, emit, lambda _event: None)
        await callback.async_log_success_event({}, {}, None, None)
        self.assertEqual(emitted, [])

    async def test_terminal_callbacks_use_whitelist_envelopes(self):
        emitted = []
        async def emit(value):
            emitted.append(value)
        callback = OSSAuditCallback(lambda _kwargs: True, emit, lambda _event: None)
        await callback.async_log_success_event({"messages": [{"content": "synthetic input"}]}, {"choices": []}, None, None)
        await callback.async_log_failure_event({"exception": RuntimeError("PRIVATE")}, None, None, None)
        self.assertEqual([item["event"] for item in emitted], ["success", "failure"])
        self.assertTrue(all(item["transportCompleteness"] == "not-verified" for item in emitted))
        self.assertEqual(emitted[1]["errorType"], "RuntimeError")

    async def test_delivery_failure_signals_no_content_and_propagates_to_litellm(self):
        events = []
        async def emit(_value):
            raise OSError("PRIVATE_STORAGE_EXCEPTION")
        callback = OSSAuditCallback(lambda _kwargs: True, emit, events.append)
        with self.assertRaisesRegex(RuntimeError, "^L3 callback delivery failed$") as caught:
            await callback.async_log_success_event({}, {}, None, None)
        self.assertNotIn("PRIVATE_", "".join(traceback.format_exception(caught.exception)))
        self.assertEqual(events, [{"event": "l3_callback_delivery_failed"}])

    async def test_signal_failure_also_hides_original_exception_text(self):
        async def emit(_value):
            raise OSError("PRIVATE_STORAGE_EXCEPTION")
        def signal(_event):
            raise ValueError("PRIVATE_SIGNAL_EXCEPTION")
        callback = OSSAuditCallback(lambda _kwargs: True, emit, signal)
        with self.assertRaisesRegex(RuntimeError, "^L3 callback delivery failed$") as caught:
            await callback.async_log_success_event({}, {}, None, None)
        self.assertNotIn("PRIVATE_", "".join(traceback.format_exception(caught.exception)))


if __name__ == "__main__":
    unittest.main()