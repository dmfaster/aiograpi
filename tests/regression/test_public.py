import time
import unittest
from unittest.mock import AsyncMock, Mock

import orjson

from aiograpi import Client
from aiograpi.exceptions import ClientGraphqlError, ClientLoginRequired


class PublicRequestRegressionTestCase(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _relay_bootstrap_html():
        return (
            '<html><script id="__eqmc" type="application/json">'
            '{"u":"/ajax/qm/?__a=1&__user=0&__comet_req=7&jazoest=26582",'
            '"e":"7335888108907652597","s":"XPolarisProfileController",'
            '"w":0,"f":null,"l":"6b2800R9u4biJOYjcdXFEIabc"}'
            '</script><script type="application/json">'
            '["SiteData",[],{"haste_session":"19999.HYP:instagram_web_pkg.2.1..0.1",'
            '"server_revision":1011444902,"hsi":"7335888108907652597",'
            '"__spin_r":1011444902,"__spin_b":"trunk","__spin_t":1708019550,'
            '"comet_env":7,"pr":1},123]'
            "</script></html>"
        )

    async def test_public_web_relay_bootstraps_once_then_reuses_live_context(self):
        client = Client(public_transport="curl")
        response = 'for (;;);{"data":{"user":{"id":"123"}}}'
        client.public_request = AsyncMock(side_effect=[self._relay_bootstrap_html(), response, response])

        first = await client.public_web_relay_request(
            "28036671149327607",
            {"id": "123"},
            referer="https://www.instagram.com/example/",
            friendly_name="PolarisProfilePageContentQuery",
        )

        self.assertEqual(first["user"]["id"], "123")
        self.assertTrue(client.public_web_relay_context_ready)
        self.assertEqual(client.last_public_web_relay_request_count, 2)
        bootstrap_call, relay_call = client.public_request.await_args_list[:2]
        self.assertFalse(bootstrap_call.kwargs["return_json"])
        self.assertEqual(relay_call.args[0], client.GRAPHQL_PUBLIC_WEB_API_URL)
        self.assertEqual(relay_call.kwargs["retries_count"], 1)
        self.assertEqual(relay_call.kwargs["data"]["__a"], "1")
        self.assertEqual(relay_call.kwargs["data"]["__hsi"], "7335888108907652597")
        self.assertEqual(relay_call.kwargs["data"]["jazoest"], "26582")
        self.assertEqual(relay_call.kwargs["data"]["doc_id"], "28036671149327607")
        self.assertEqual(relay_call.kwargs["headers"]["X-FB-LSD"], "6b2800R9u4biJOYjcdXFEIabc")
        self.assertEqual(relay_call.kwargs["headers"]["Sec-Fetch-Site"], "same-origin")
        self.assertEqual(relay_call.kwargs["headers"]["Sec-Ch-Ua-Mobile"], "?0")
        self.assertEqual(relay_call.kwargs["headers"]["X-IG-Max-Touch-Points"], "0")

        second = await client.public_web_relay_request(
            "28036671149327607",
            {"id": "124"},
            referer="https://www.instagram.com/another.example/",
            friendly_name="PolarisProfilePageContentQuery",
        )

        self.assertEqual(second["user"]["id"], "123")
        self.assertEqual(client.last_public_web_relay_request_count, 1)
        self.assertEqual(client.public_request.await_count, 3)
        self.assertEqual(client.public_request.await_args.kwargs["data"]["__req"], "2")

    async def test_public_web_relay_rejects_authenticated_cookie_state(self):
        client = Client(public_transport="curl")
        client.public.set_cookies({"sessionid": "must-not-leak"})
        client.public_request = AsyncMock()

        with self.assertRaises(ClientGraphqlError):
            await client.public_web_relay_request(
                "28036671149327607",
                {"id": "123"},
                referer="https://www.instagram.com/example/",
                friendly_name="PolarisProfilePageContentQuery",
            )

        client.public_request.assert_not_awaited()

    async def test_public_web_relay_failure_clears_cached_context_without_hidden_retry(self):
        client = Client(public_transport="curl")
        client.public_request = AsyncMock(
            side_effect=[self._relay_bootstrap_html(), '{"errors":[{"message":"rotated"}]}']
        )

        with self.assertRaises(ClientGraphqlError):
            await client.public_web_relay_request(
                "28036671149327607",
                {"id": "123"},
                referer="https://www.instagram.com/example/",
                friendly_name="PolarisProfilePageContentQuery",
            )

        self.assertFalse(client.public_web_relay_context_ready)
        self.assertEqual(client.public_request.await_count, 2)

    async def test_public_web_relay_requires_browser_impersonating_transport(self):
        client = Client(public_transport="requests")
        client.public_request = AsyncMock()

        with self.assertRaises(ClientGraphqlError):
            await client.public_web_relay_request(
                "28036671149327607",
                {"id": "123"},
                referer="https://www.instagram.com/example/",
                friendly_name="PolarisProfilePageContentQuery",
            )

        client.public_request.assert_not_awaited()

    async def test_proxy_change_discards_public_web_relay_context(self):
        client = Client(public_transport="curl")
        client._public_web_relay_context = {
            "fetched_at": time.monotonic(),
        }

        client.set_proxy("http://first.invalid:10001")
        self.assertFalse(client.public_web_relay_context_ready)

        client._public_web_relay_context = {
            "fetched_at": time.monotonic(),
        }
        client.set_proxy("http://second.invalid:10002")
        self.assertFalse(client.public_web_relay_context_ready)

    async def test_public_request_maps_challenge_redirect_html_to_login_required(self):
        client = Client()
        client.last_response_ts = 0
        response = Mock()
        response.status_code = 200
        response.url = "https://www.instagram.com/challenge/?next=/graphql/query/"
        response.text = '<!DOCTYPE html><html lang="en" class="no-js logged-in client-root"></html>'
        response.raise_for_status.return_value = None
        response.json.side_effect = orjson.JSONDecodeError("unexpected character", response.text, 0)
        client.public.get = AsyncMock(return_value=response)

        with self.assertRaises(ClientLoginRequired):
            await client._send_public_request("https://www.instagram.com/graphql/query/", return_json=True)

    async def test_public_doc_id_graphql_request_injects_logged_in_public_cookies(self):
        client = Client()
        client.authorization_data = {"sessionid": "123:session", "ds_user_id": "123"}
        client.public.set_cookies({"csrftoken": "csrf-token"})
        client.public_request = AsyncMock(return_value={"data": {"ok": True}})

        result = await client.public_doc_id_graphql_request("27128499623469141", {"shortcode": "abc"})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.public.cookies_dict()["sessionid"], "123:session")
        headers = client.public_request.await_args.kwargs["headers"]
        self.assertEqual(headers["X-CSRFToken"], "csrf-token")

    async def test_public_doc_id_graphql_request_forwards_exact_attempt_budget(self):
        client = Client()
        client.public_request = AsyncMock(return_value={"data": {"ok": True}})

        await client.public_doc_id_graphql_request(
            "37158170193798755",
            {"userID": "123"},
            retries_count=1,
        )

        self.assertEqual(client.public_request.await_args.kwargs["retries_count"], 1)

    async def test_public_doc_id_graphql_request_uses_one_authenticated_web_envelope(self):
        client = Client()
        client.authorization_data = {"sessionid": "123:session", "ds_user_id": "123"}
        client.private.set_cookies({"csrftoken": "private-csrf-token"})
        client.public_request = AsyncMock(return_value={"data": {"ok": True}})

        result = await client.public_doc_id_graphql_request(
            "37158170193798755",
            {"userID": "123"},
            retries_count=1,
            friendly_name="usePolarisGetFollowListQuery",
            web_headers=True,
        )

        self.assertEqual(result, {"ok": True})
        client.public_request.assert_awaited_once()
        self.assertEqual(
            client.public_request.await_args.args[0],
            client.GRAPHQL_PUBLIC_WEB_API_URL,
        )
        kwargs = client.public_request.await_args.kwargs
        self.assertEqual(kwargs["retries_count"], 1)
        self.assertEqual(kwargs["data"]["fb_api_caller_class"], "RelayModern")
        self.assertEqual(
            kwargs["data"]["fb_api_req_friendly_name"],
            "usePolarisGetFollowListQuery",
        )
        self.assertEqual(kwargs["headers"]["User-Agent"], client.public_user_agent)
        self.assertEqual(kwargs["headers"]["Origin"], "https://www.instagram.com")
        self.assertEqual(kwargs["headers"]["X-ASBD-ID"], "359341")
        self.assertEqual(kwargs["headers"]["X-IG-App-ID"], "936619743392459")
        self.assertEqual(
            kwargs["headers"]["X-FB-Friendly-Name"],
            "usePolarisGetFollowListQuery",
        )
        self.assertEqual(kwargs["headers"]["X-CSRFToken"], "private-csrf-token")
        self.assertEqual(kwargs["data"]["av"], "123")
        self.assertEqual(client.public.cookies_dict()["csrftoken"], "private-csrf-token")

    async def test_public_doc_id_graphql_request_keeps_legacy_endpoint_without_web_envelope(self):
        client = Client()
        client.public_request = AsyncMock(return_value={"data": {"ok": True}})

        await client.public_doc_id_graphql_request(
            "27128499623469141",
            {"shortcode": "abc"},
            retries_count=1,
        )

        self.assertEqual(
            client.public_request.await_args.args[0],
            client.GRAPHQL_PUBLIC_API_URL,
        )

    async def test_public_doc_id_graphql_request_rejects_untrusted_metadata_before_network(self):
        client = Client()
        client.public_request = AsyncMock()

        with self.assertRaises(ValueError):
            await client.public_doc_id_graphql_request("not-a-doc-id", {})
        with self.assertRaises(ValueError):
            await client.public_doc_id_graphql_request(
                "37158170193798755",
                {},
                friendly_name="bad friendly name!",
            )

        client.public_request.assert_not_awaited()

    async def test_public_doc_id_graphql_request_posts_web_api_with_lsd(self):
        client = Client()
        client.public.set_cookies({"csrftoken": "csrf-token"})
        html = '<html><script>["LSD",[],{"token":"lsd-token"}]</script></html>'
        client.public_request = AsyncMock(side_effect=[html, {"data": {"ok": True}}])

        result = await client.public_doc_id_graphql_request(
            "27128499623469141",
            {"shortcode": "DaHEdwgogl4"},
            referer="https://www.instagram.com/p/DaHEdwgogl4/",
            url=client.GRAPHQL_PUBLIC_WEB_API_URL,
            include_lsd=True,
            headers={"X-FB-Friendly-Name": "PolarisPostRootQuery"},
        )

        self.assertEqual(result, {"ok": True})
        bootstrap_call, query_call = client.public_request.await_args_list
        self.assertEqual(bootstrap_call.args[0], "https://www.instagram.com/p/DaHEdwgogl4/")
        self.assertFalse(bootstrap_call.kwargs["return_json"])
        self.assertEqual(query_call.args[0], client.GRAPHQL_PUBLIC_WEB_API_URL)
        kwargs = query_call.kwargs
        self.assertEqual(kwargs["data"]["doc_id"], "27128499623469141")
        self.assertEqual(kwargs["data"]["variables"], '{"shortcode":"DaHEdwgogl4"}')
        self.assertEqual(kwargs["data"]["lsd"], "lsd-token")
        self.assertEqual(kwargs["headers"]["X-FB-LSD"], "lsd-token")
        self.assertEqual(kwargs["headers"]["X-CSRFToken"], "csrf-token")
        self.assertEqual(kwargs["headers"]["X-FB-Friendly-Name"], "PolarisPostRootQuery")
        self.assertEqual(kwargs["headers"]["X-ASBD-ID"], "359341")
        self.assertEqual(kwargs["headers"]["X-IG-App-ID"], "936619743392459")
        self.assertEqual(kwargs["headers"]["X-Requested-With"], "XMLHttpRequest")
        self.assertFalse(kwargs["update_headers"])
        self.assertTrue(kwargs["return_json"])
