import unittest
from unittest.mock import MagicMock

from app.services.core.config import AnythingLLMConfig
from app.services.utils.anythingllm_client import AnythingLLMClient


class AnythingLLMClientStreamTests(unittest.TestCase):
    def test_stream_chat_to_thread_uses_low_buffering_for_upstream_sse(self):
        client = AnythingLLMClient(
            AnythingLLMConfig(
                base_url="http://anythingllm.local",
                api_key="test-key",
                timeout=30,
                storage_root=None,
            )
        )
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.iter_lines.return_value = iter([
            'event: textResponseChunk',
            'data: {"type":"textResponseChunk","textResponse":"你"}',
            "",
            'event: textResponseChunk',
            'data: {"type":"textResponseChunk","textResponse":"好"}',
            "",
            'event: textResponse',
            'data: {"type":"textResponse","textResponse":"！","close":true}',
            "",
        ])
        client.session.post = MagicMock(return_value=mock_response)

        chunks = list(client.stream_chat_to_thread("workspace-1", "thread-1", "你好"))

        self.assertEqual(chunks, ["你", "好", "！"])
        mock_response.iter_lines.assert_called_once_with(decode_unicode=True, chunk_size=1)
        mock_response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
