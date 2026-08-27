import unittest
from unittest.mock import AsyncMock

from aiograpi import Client


class PrivateGraphqlObservabilityRegressionTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_transport_failure_clears_previous_response_state(self):
        client = Client()
        previous_response = object()
        client.last_response = previous_response
        client.last_json = {"previous": "response"}
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


if __name__ == "__main__":
    unittest.main()
