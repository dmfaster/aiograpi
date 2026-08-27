import unittest
from unittest.mock import AsyncMock, Mock

from aiograpi import Client


class PrivateGraphqlObservabilityRegressionTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_query_request_records_exactly_one_attempt_and_safe_response_state(self):
        client = Client()
        client.private_requests_count = 4
        client.last_response_ts = 0
        client.request_log = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "xdt_api__v1__friendships__followers": {
                    "users": [],
                }
            }
        }
        client.private.post = AsyncMock(return_value=response)

        result = await client.private_graphql_query_request(
            friendly_name="FollowersList",
            root_field_name="xdt_api__v1__friendships__followers",
            variables={"user_id": "1"},
            client_doc_id="123",
        )

        self.assertEqual(client.private_requests_count, 5)
        self.assertIs(client.last_response, response)
        self.assertIs(client.last_json, result)
        self.assertGreater(client.last_response_ts, 0)
        client.request_log.assert_called_once_with(response)
        client.private.post.assert_awaited_once()

    async def test_query_request_retains_partial_graphql_error_envelope_for_caller(self):
        client = Client()
        client.request_log = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "xdt_api__v1__friendships__followers": {
                    "users": [{"pk": "1", "username": "one"}],
                }
            },
            "errors": [{"message": "safe-test-error"}],
        }
        client.private.post = AsyncMock(return_value=response)

        result = await client.private_graphql_query_request(
            friendly_name="FollowersList",
            root_field_name="xdt_api__v1__friendships__followers",
            variables={"user_id": "1"},
            client_doc_id="123",
        )

        self.assertEqual(result["errors"], [{"message": "safe-test-error"}])
        self.assertEqual(result["data"]["xdt_api__v1__friendships__followers"]["users"][0]["pk"], "1")

    async def test_transport_failure_clears_previous_response_state(self):
        client = Client()
        previous_response = object()
        client.last_response = previous_response
        client.last_json = {"previous": "response"}
        client.private_requests_count = 0
        client.last_response_ts = 0
        client.private.post = AsyncMock(side_effect=RuntimeError("network unavailable"))

        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            await client.private_graphql_query_request(
                friendly_name="FollowersList",
                root_field_name="xdt_api__v1__friendships__followers",
                variables={"user_id": "1"},
                client_doc_id="123",
            )

        self.assertIsNone(client.last_response)
        self.assertEqual(client.last_json, {})
        self.assertEqual(client.private_requests_count, 1)
        self.assertGreater(client.last_response_ts, 0)


if __name__ == "__main__":
    unittest.main()
