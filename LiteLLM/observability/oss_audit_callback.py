"""Opt-in OSS callback; requires a separately authorized, durable sink."""

from litellm.integrations.custom_logger import CustomLogger

from LiteLLM.observability.audit_envelope import build_envelope


class OSSAuditCallback(CustomLogger):
    def __init__(self, select_request, emit, signal, max_content_bytes=2 * 1024 * 1024):
        super().__init__()
        self.select_request = select_request
        self.emit = emit
        self.signal = signal
        self.max_content_bytes = max_content_bytes

    async def publish(self, event, kwargs, response_obj):
        try:
            if not self.select_request(kwargs):
                return
            envelope = build_envelope(event, kwargs, response_obj, self.max_content_bytes)
            await self.emit(envelope)
        except Exception:
            try:
                self.signal({"event": "l3_callback_delivery_failed"})
            finally:
                raise RuntimeError("L3 callback delivery failed") from None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        await self.publish("success", kwargs, response_obj)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        await self.publish("failure", kwargs, response_obj)