from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agents import _client  # noqa: E402


class _StreamingResponse:
    ok = True
    status_code = 200

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self):
        return iter(self._lines)


class HuggingFaceClientTests(unittest.TestCase):
    @patch("requests.post")
    def test_single_turn_call_uses_hf_chat_completions(self, post: Mock):
        response = Mock(ok=True)
        response.json.return_value = {
            "choices": [{"message": {"content": "  ready  "}}]
        }
        post.return_value = response

        with patch.dict("os.environ", {"HF_TOKEN": "test-token"}):
            result = _client._huggingface_call("system prompt", "status?")

        self.assertEqual(result, "ready")
        kwargs = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], _client._HF_API_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(kwargs["json"]["model"], _client._HF_MODEL)
        self.assertEqual(
            kwargs["json"]["messages"],
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "status?"},
            ],
        )

    @patch("requests.post")
    def test_streaming_call_yields_content_chunks(self, post: Mock):
        post.return_value = _StreamingResponse([
            b'data: {"choices":[{"delta":{"content":"Fire"}}]}',
            b'data: {"choices":[{"delta":{"content":" contained"}}]}',
            b'data: [DONE]',
        ])

        with patch.dict("os.environ", {"HF_TOKEN": "test-token"}):
            chunks = list(_client._huggingface_stream(
                "system prompt",
                [{"role": "user", "content": "status?"}],
            ))

        self.assertEqual(chunks, ["Fire", " contained"])
        kwargs = post.call_args.kwargs
        self.assertTrue(kwargs["json"]["stream"])
        self.assertTrue(kwargs["stream"])

    def test_public_interface_dispatches_huggingface_aliases(self):
        with (
            patch.object(_client, "_PROVIDER", "huggingface"),
            patch.object(_client, "_huggingface_call", return_value="ok") as call,
        ):
            self.assertEqual(_client.call_llm("system", "user"), "ok")
            call.assert_called_once_with("system", "user")

        with (
            patch.object(_client, "_PROVIDER", "hf"),
            patch.object(
                _client, "_huggingface_stream", return_value=iter(["a", "b"])
            ) as stream,
        ):
            self.assertEqual(
                list(_client.stream_llm("system", [{"role": "user", "content": "u"}])),
                ["a", "b"],
            )
            stream.assert_called_once()


if __name__ == "__main__":
    unittest.main()
