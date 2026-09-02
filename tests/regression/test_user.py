import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiograpi import Client
from aiograpi import types as ig_types
from aiograpi.exceptions import ClientError, ClientGraphqlError, ClientJSONDecodeError, UserNotFound
from aiograpi.extractors import extract_user_gql, extract_user_short, extract_user_v1
from aiograpi.mixins.user import (
    FOLLOWERS_LIST_CURRENT_CLIENT_DOC_ID,
    MAX_PUBLIC_GRAPHQL_USER_COUNT,
    MAX_USER_COUNT,
    PUBLIC_FOLLOWERS_QUERY_HASH,
    PUBLIC_FOLLOWING_QUERY_HASH,
    PUBLIC_WEB_FOLLOWERS_CONNECTION,
    PUBLIC_WEB_FOLLOWERS_DOC_ID,
    PUBLIC_WEB_FOLLOWERS_FRIENDLY_NAME,
    PUBLIC_WEB_FOLLOWING_CONNECTION,
    PUBLIC_WEB_PROFILE_DOC_ID,
    PUBLIC_WEB_PROFILE_FRIENDLY_NAME,
    USER_INFO_BY_USERNAME_V2_DOC_ID,
    USER_INFO_V2_DOC_ID,
    UserMixin,
)
from aiograpi.types import UserShort


class UserMixinRegressionTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_user_info_by_id_public_relay_uses_current_anonymous_profile_operation(self):
        client = Client(public_transport="curl")
        client.public_web_relay_request = AsyncMock(
            return_value={
                "user": {
                    "id": "25025320",
                    "username": "instagram",
                    "full_name": "Instagram",
                    "is_private": False,
                    "is_verified": True,
                    "media_count": None,
                    "follower_count": 2,
                    "following_count": 3,
                    "is_business_account": False,
                    "profile_pic_url": "https://example.com/pic.jpg",
                }
            }
        )

        user = await client.user_info_by_id_public_relay(
            "25025320",
            " @Instagram ",
            expected_request_count=2,
        )

        self.assertEqual(user.pk, "25025320")
        self.assertEqual(user.username, "instagram")
        self.assertEqual(user.media_count, 0)
        request = client.public_web_relay_request.await_args
        self.assertEqual(request.args[0], PUBLIC_WEB_PROFILE_DOC_ID)
        self.assertEqual(request.args[1]["id"], "25025320")
        self.assertNotIn("render_surface", request.args[1])
        self.assertEqual(
            set(request.args[1]),
            {
                "id",
                "enable_integrity_filters",
                "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider",
                "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider",
                "__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider",
                "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider",
                "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider",
            },
        )
        self.assertEqual(request.kwargs["referer"], "https://www.instagram.com/instagram/")
        self.assertEqual(request.kwargs["friendly_name"], PUBLIC_WEB_PROFILE_FRIENDLY_NAME)
        self.assertEqual(request.kwargs["retries_count"], 1)
        self.assertEqual(request.kwargs["expected_request_count"], 2)

    def test_current_followers_document_candidate_is_explicit_and_pinned(self):
        self.assertRegex(FOLLOWERS_LIST_CURRENT_CLIENT_DOC_ID, r"^[0-9]{20,40}$")

    def _build_private_client(self):
        client = Client()
        client._user_id = "1"
        client.uuid = "uuid"
        client.with_action_data = lambda data: data
        return client

    def _build_action_client(self):
        client = Client()
        client._user_id = "1"
        client.uuid = "uuid"
        client.android_device_id = "android-device"
        return client

    async def test_user_caches_are_isolated_between_clients(self):
        first = Client()
        second = Client()

        first._users_cache["1"] = Mock(username="first")
        first._usernames_cache["first"] = "1"
        first._users_followers["1"] = {"2": Mock(username="follower")}

        self.assertEqual(second._users_cache, {})
        self.assertEqual(second._usernames_cache, {})
        self.assertEqual(second._users_followers, {})

    async def test_username_from_user_id_fallback_awaits_user_info(self):
        client = Client()
        client.username_from_user_id_gql = AsyncMock(side_effect=ClientError("graphql failed"))
        client.user_info_v1 = AsyncMock(return_value=Mock(username="fallback_user"))

        username = await client.username_from_user_id(123)

        self.assertEqual(username, "fallback_user")
        client.username_from_user_id_gql.assert_awaited_once_with("123")
        client.user_info_v1.assert_awaited_once_with("123")

    async def test_user_id_from_username_v1_once_disables_hidden_retries(self):
        client = Client()
        client.private_request = AsyncMock(return_value={"user": {"pk": "123"}})

        user_id = await client.user_id_from_username_v1_once(" @Example ")

        self.assertEqual(user_id, "123")
        client.private_request.assert_awaited_once_with(
            "users/example/usernameinfo/",
            retry_transient=False,
            retry_without_cursor=False,
        )

    async def test_username_from_user_id_uses_private_first_when_authorized(self):
        client = Client()
        client.authorization_data = {"sessionid": "sessionid-value", "ds_user_id": "1"}
        client.user_info_v1 = AsyncMock(return_value=Mock(username="private_user"))
        client.username_from_user_id_gql = AsyncMock(
            side_effect=AssertionError("authorized lookup should use private first")
        )

        username = await client.username_from_user_id("123")

        self.assertEqual(username, "private_user")
        client.user_info_v1.assert_awaited_once_with("123")
        client.username_from_user_id_gql.assert_not_awaited()

    async def test_user_info_uses_private_first_when_authorized(self):
        client = Client()
        client.authorization_data = {"sessionid": "sessionid-value", "ds_user_id": "1"}
        client._users_cache = {}
        client._usernames_cache = {}
        private_user = Mock(pk="123", username="private_user")
        client.user_info_v1 = AsyncMock(return_value=private_user)
        client._user_info_public = AsyncMock(side_effect=AssertionError("authorized lookup should use private first"))

        user = await client.user_info("123")

        self.assertEqual(user.username, "private_user")
        client.user_info_v1.assert_awaited_once_with("123")
        client._user_info_public.assert_not_awaited()

    async def test_user_info_by_username_uses_private_first_when_authorized(self):
        client = Client()
        client.authorization_data = {"sessionid": "sessionid-value", "ds_user_id": "1"}
        client._users_cache = {}
        client._usernames_cache = {}
        private_user = Mock(pk="123", username="example")
        client.user_info_by_username_v1 = AsyncMock(return_value=private_user)
        client._user_info_by_username_public = AsyncMock(
            side_effect=AssertionError("authorized username lookup should use private first")
        )

        user = await client.user_info_by_username(" @Example ")

        self.assertEqual(user.username, "example")
        client.user_info_by_username_v1.assert_awaited_once_with("example")
        client._user_info_by_username_public.assert_not_awaited()

    async def test_user_short_gql_uses_web_profile_doc_id_without_legacy_query_hash(self):
        client = Client()
        web_user = {
            "id": "25025320",
            "username": "instagram",
            "full_name": "Instagram",
            "is_private": False,
            "profile_pic_url": "https://example.com/pic.jpg",
        }
        client.public_graphql_request = AsyncMock(side_effect=AssertionError("legacy query_hash should not be used"))
        client.user_web_profile_info_gql = AsyncMock(return_value=web_user)

        user = await client.user_short_gql("25025320")

        self.assertEqual(user.username, "instagram")
        client.user_web_profile_info_gql.assert_awaited_once_with("25025320")
        client.public_graphql_request.assert_not_called()

    async def test_extract_user_short_preserves_follower_payload_fields(self):
        payload = {
            "pk": 123,
            "id": "123",
            "username": "follower",
            "full_name": "Follower",
            "is_private": False,
            "is_verified": True,
            "latest_reel_media": 1710000123,
            "has_anonymous_profile_picture": False,
            "profile_pic_url": "https://example.com/pic.jpg",
        }

        user = extract_user_short(payload)

        self.assertEqual(user.pk, "123")
        self.assertTrue(user.is_verified)
        self.assertEqual(user.latest_reel_media, 1710000123)
        self.assertFalse(user.has_anonymous_profile_picture)

    async def test_extract_user_short_preserves_private_graphql_v2_fields(self):
        payload = {
            "id": "123",
            "strong_id__": "123",
            "username": "follower",
            "full_name": "Follower",
            "is_private": False,
            "is_verified": True,
            "1llatest_reel_media": 1710000123,
            "account_badges": [{"badge_type": "example"}],
            "fbid_v2": 17841400000000000,
            "friendship_status": {
                "following": True,
                "incoming_request": False,
                "is_bestie": False,
                "is_feed_favorite": True,
                "is_private": False,
                "is_restricted": False,
                "outgoing_request": False,
            },
            "has_anonymous_profile_picture": False,
            "interop_messaging_user_fbid": 117943452927407,
            "profile_pic_id": "3725434617063385984_123",
            "profile_pic_url": "https://example.com/pic.jpg",
        }

        user = extract_user_short(payload)

        self.assertEqual(user.pk, "123")
        self.assertEqual(user.latest_reel_media, 1710000123)
        self.assertEqual(user.profile_pic_id, "3725434617063385984_123")
        self.assertEqual(user.fbid_v2, "17841400000000000")
        self.assertEqual(user.interop_messaging_user_fbid, "117943452927407")
        self.assertEqual(user.strong_id__, "123")
        self.assertEqual(user.account_badges, [{"badge_type": "example"}])
        self.assertEqual(user.friendship_status.user_id, "123")
        self.assertTrue(user.friendship_status.following)
        self.assertTrue(user.friendship_status.is_feed_favorite)

    async def test_user_web_profile_info_gql_uses_public_doc_id_endpoint(self):
        client = Client()
        web_user = {
            "id": "25025320",
            "username": "instagram",
            "full_name": "Instagram",
            "is_private": False,
            "profile_pic_url": "https://example.com/pic.jpg",
        }
        client.inject_sessionid_to_public = Mock(return_value=True)
        client.public_request = AsyncMock(side_effect=AssertionError("legacy /api/graphql endpoint should not be used"))
        client.public_doc_id_graphql_request = AsyncMock(return_value={"user": web_user})

        user = await client.user_web_profile_info_gql("25025320")

        self.assertEqual(user["username"], "instagram")
        client.public_request.assert_not_called()
        client.public_doc_id_graphql_request.assert_awaited_once()
        args, kwargs = client.public_doc_id_graphql_request.call_args
        self.assertEqual(args[0], "26762473490008061")
        self.assertEqual(args[1]["id"], "25025320")
        self.assertEqual(args[1]["render_surface"], "PROFILE")
        self.assertEqual(kwargs["referer"], "https://www.instagram.com/25025320/")

    async def test_user_info_by_username_gql_normalizes_username(self):
        class DummyClient(UserMixin):
            response_body = None

            def __init__(self):
                self.public_request_calls = []

            async def public_request(self, url, headers=None, **kwargs):
                self.public_request_calls.append({"url": url, "headers": headers, "kwargs": kwargs})
                return json.dumps(self.response_body)

        client = DummyClient()
        client.response_body = {
            "data": {
                "user": {
                    "id": "123",
                    "username": "example",
                    "full_name": "Example",
                    "is_private": False,
                    "is_verified": False,
                    "profile_pic_url": "https://example.com/pic.jpg",
                    "profile_pic_url_hd": None,
                    "edge_owner_to_timeline_media": {"count": 0},
                    "edge_followed_by": {"count": 0},
                    "edge_follow": {"count": 0},
                    "is_business_account": False,
                    "business_email": None,
                    "business_phone_number": None,
                    "biography": "",
                    "bio_links": [],
                    "external_url": None,
                    "business_category_name": None,
                    "category_name": None,
                    "fbid": "123",
                    "pinned_channels_info": {"pinned_channels_list": []},
                }
            }
        }

        user = await client.user_info_by_username_gql(" @Example ")

        self.assertEqual(user.username, "example")
        self.assertIn("web_profile_info/?username=example", client.public_request_calls[0]["url"])

    async def test_user_info_by_username_v1_normalizes_username(self):
        client = Client()
        client.private_request = AsyncMock(
            return_value={
                "user": {
                    "pk": "123",
                    "username": "example",
                    "full_name": "Example",
                    "is_private": False,
                    "is_verified": False,
                    "profile_pic_url": "https://example.com/pic.jpg",
                    "media_count": 0,
                    "follower_count": 0,
                    "following_count": 0,
                    "is_business": False,
                }
            }
        )

        user = await client.user_info_by_username_v1(" @Example ")

        self.assertEqual(user.username, "example")
        client.private_request.assert_awaited_once_with("users/example/usernameinfo/")

    async def test_extract_user_v1_maps_business_contact_fields(self):
        user = extract_user_v1(
            {
                "pk": "123",
                "username": "business",
                "full_name": "Business",
                "is_private": False,
                "profile_pic_url": "https://example.com/pic.jpg",
                "is_verified": False,
                "media_count": 0,
                "follower_count": 0,
                "following_count": 0,
                "is_business": True,
                "business_email": "public@example.com",
                "business_phone_number": "+15551234567",
                "external_url": "",
            }
        )

        self.assertEqual(user.public_email, "public@example.com")
        self.assertEqual(user.contact_phone_number, "+15551234567")

    async def test_extract_user_v1_preserves_threads_badge_fields(self):
        user = extract_user_v1(
            {
                "pk": "123",
                "username": "creator",
                "full_name": "Creator",
                "is_private": False,
                "profile_pic_url": "https://example.com/pic.jpg",
                "is_verified": False,
                "media_count": 0,
                "follower_count": 0,
                "following_count": 0,
                "is_business": False,
                "show_text_post_app_badge": True,
                "text_post_app_badge_label": "creator_threads",
            }
        )

        self.assertTrue(user.show_text_post_app_badge)
        self.assertEqual(user.text_post_app_badge_label, "creator_threads")

    async def test_extract_user_gql_preserves_threads_badge_fields(self):
        user = extract_user_gql(
            {
                "id": "123",
                "username": "creator",
                "full_name": "Creator",
                "is_private": False,
                "profile_pic_url": "https://example.com/pic.jpg",
                "profile_pic_url_hd": None,
                "is_verified": False,
                "edge_owner_to_timeline_media": {"count": 0},
                "edge_followed_by": {"count": 0},
                "edge_follow": {"count": 0},
                "is_business_account": False,
                "business_email": None,
                "business_phone_number": None,
                "biography": "",
                "bio_links": [],
                "external_url": None,
                "business_category_name": None,
                "category_name": None,
                "fbid": "123",
                "pinned_channels_info": {"pinned_channels_list": []},
                "show_text_post_app_badge": True,
                "text_post_app_badge_label": "creator_threads",
            }
        )

        self.assertTrue(user.show_text_post_app_badge)
        self.assertEqual(user.text_post_app_badge_label, "creator_threads")

    async def test_user_info_by_username_v2_gql_normalizes_search_query(self):
        client = Client()
        client._inject_sessionid_for_v2_gql = Mock()
        client.public_doc_id_graphql_request = AsyncMock(
            return_value={"xdt_api__v1__fbsearch__non_profiled_serp": {"users": [{"username": "example", "pk": "123"}]}}
        )
        client.user_info_v2_gql = AsyncMock(return_value="user")

        result = await client.user_info_by_username_v2_gql(" @Example ")

        self.assertEqual(result, "user")
        client.public_doc_id_graphql_request.assert_awaited_once_with(
            USER_INFO_BY_USERNAME_V2_DOC_ID, {"hasQuery": True, "query": "example"}
        )

    async def test_user_info_v2_gql_uses_profile_doc_id(self):
        client = Client()
        client._inject_sessionid_for_v2_gql = Mock()
        client.public_doc_id_graphql_request = AsyncMock(
            return_value={
                "user": {
                    "id": "25025320",
                    "username": "instagram",
                    "full_name": "Instagram",
                    "is_private": False,
                    "is_verified": True,
                    "profile_pic_url": "https://example.com/pic.jpg",
                    "profile_pic_url_hd": None,
                    "media_count": 0,
                    "follower_count": 0,
                    "following_count": 0,
                    "is_business": False,
                }
            }
        )

        user = await client.user_info_v2_gql("25025320")

        client.public_doc_id_graphql_request.assert_awaited_once()
        self.assertEqual(client.public_doc_id_graphql_request.await_args.args[0], USER_INFO_V2_DOC_ID)
        self.assertEqual(client.public_doc_id_graphql_request.await_args.args[1]["id"], "25025320")
        self.assertEqual(user.pk, "25025320")

    async def test_user_stream_by_username_v1_normalizes_endpoint(self):
        client = Client()
        client.private_request = AsyncMock(return_value={"stream_rows": []})

        await client.user_stream_by_username_v1(" @Example ")

        client.private_request.assert_awaited_once()
        self.assertEqual(client.private_request.call_args.args[0], "users/example/usernameinfo_stream/")

    async def test_user_web_profile_info_v1_normalizes_username_param(self):
        client = Client()
        client.private_request = AsyncMock(return_value={"data": {"pk": "9", "username": "example"}})

        user = await client.user_web_profile_info_v1(" @Example ")

        client.private_request.assert_awaited_once_with("users/web_profile_info/", params={"username": "example"})
        self.assertEqual(user, {"pk": "9", "username": "example"})

    async def test_user_followers_v1_chunk_sends_order(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_request = AsyncMock(return_value={"users": [], "next_max_id": None})

        await client.user_followers_v1_chunk("123", max_amount=2, order="date_followed_latest")

        client.private_request.assert_awaited_once()
        self.assertEqual(client.private_request.call_args.kwargs["params"]["order"], "date_followed_latest")

    async def test_user_followers_v1_page_makes_one_request_and_returns_cursor(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_request = AsyncMock(
            return_value={
                "users": [{"pk": "1", "username": "one"}],
                "next_max_id": "cursor-2",
            }
        )

        users, cursor = await client.user_followers_v1_page(
            "123",
            max_id="cursor-1",
            count=50,
            order="date_followed_latest",
        )

        self.assertEqual([user.pk for user in users], ["1"])
        self.assertEqual(cursor, "cursor-2")
        client.private_request.assert_awaited_once_with(
            "friendships/123/followers/",
            params={
                "count": 50,
                "rank_token": "rank-token",
                "search_surface": "follow_list_page",
                "query": "",
                "enable_groups": "true",
                "order": "date_followed_latest",
                "max_id": "cursor-1",
            },
            retry_transient=False,
            retry_without_cursor=False,
        )

    async def test_user_followers_v1_page_result_accepts_max_id_and_records_safe_shape(self):
        client = Client()
        client.uuid = "rank-token"
        client.last_response = SimpleNamespace(status_code=200, content=b"safe-count-only")
        client.private_request = AsyncMock(
            return_value={
                "users": [{"pk": "1", "username": "one"}],
                "max_id": "cursor-2",
                "has_more": True,
                "status": "ok",
                "123456789": "dynamic-key-is-not-shape-metadata",
            }
        )

        page = await client.user_followers_v1_page_result("123", count=100)

        self.assertEqual(page.next_cursor, "cursor-2")
        self.assertEqual(page.cursor_field, "max_id")
        self.assertIs(page.has_more, True)
        self.assertEqual(page.raw_user_count, 1)
        self.assertEqual(page.http_status, 200)
        self.assertEqual(page.response_bytes, 15)
        self.assertEqual(page.response_keys, ("has_more", "max_id", "status", "users"))
        self.assertNotIn("cursor-2", page.response_keys)

    async def test_user_followers_v1_page_result_treats_zero_cursor_as_exhaustion(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_request = AsyncMock(return_value={"users": [], "next_max_id": "0", "has_more": False})

        page = await client.user_followers_v1_page_result("123", count=100)

        self.assertEqual(page.next_cursor, "")
        self.assertEqual(page.cursor_field, "")
        self.assertIs(page.has_more, False)

    async def test_user_followers_gql_page_result_fetches_exactly_one_relay_page(self):
        client = Client()
        client.last_response = SimpleNamespace(status_code=418, content=b"stale-private-response")
        client.last_public_response = SimpleNamespace(status_code=200, content=b"relay-response")
        client.public_graphql_request = AsyncMock(
            return_value={
                "user": {
                    "edge_followed_by": {
                        "edges": [
                            {"node": {"id": "1", "username": "one"}},
                            {"node": {"id": "2", "username": "two"}},
                        ],
                        "page_info": {
                            "has_next_page": True,
                            "end_cursor": "opaque-relay-cursor",
                        },
                    }
                },
                "status": "ok",
            }
        )

        page = await client.user_followers_gql_page_result(
            "123",
            end_cursor="previous-opaque-cursor",
            count=50,
        )

        self.assertEqual([user.pk for user in page.users], ["1", "2"])
        self.assertEqual(page.next_cursor, "opaque-relay-cursor")
        self.assertEqual(page.cursor_field, "page_info.end_cursor")
        self.assertIs(page.has_more, True)
        self.assertEqual(page.route, "public_graphql")
        self.assertEqual(page.raw_user_count, 2)
        self.assertEqual(page.http_status, 200)
        self.assertEqual(page.response_bytes, len(b"relay-response"))
        self.assertEqual(page.response_keys, ("status", "user"))
        self.assertEqual(page.root_keys, ("edges", "page_info"))
        client.public_graphql_request.assert_awaited_once_with(
            {
                "id": "123",
                "include_reel": True,
                "fetch_mutual": False,
                "first": 50,
                "after": "previous-opaque-cursor",
            },
            query_hash=PUBLIC_FOLLOWERS_QUERY_HASH,
            retries_count=1,
        )

    async def test_user_followers_gql_page_result_does_not_emit_exhausted_cursor(self):
        client = Client()
        client.public_graphql_request = AsyncMock(
            return_value={
                "user": {
                    "edge_followed_by": {
                        "edges": [],
                        "page_info": {
                            "has_next_page": False,
                            "end_cursor": "provider-sent-but-exhausted",
                        },
                    }
                }
            }
        )

        page = await client.user_followers_gql_page_result("123")

        self.assertEqual(page.next_cursor, "")
        self.assertEqual(page.cursor_field, "")
        self.assertIs(page.has_more, False)

    async def test_user_followers_web_gql_page_result_uses_current_persisted_operation_once(self):
        client = Client()
        client.last_public_response = SimpleNamespace(status_code=200, content=b"web-relay-response")
        client.public_doc_id_graphql_request = AsyncMock(
            return_value={
                PUBLIC_WEB_FOLLOWERS_CONNECTION: {
                    "edges": [
                        {"node": {"id": "1", "username": "one"}},
                        {"node": {"id": "2", "username": "two"}},
                    ],
                    "page_info": {
                        "has_next_page": True,
                        "end_cursor": "opaque-web-relay-cursor",
                    },
                    "should_limit_list_of_followers": False,
                }
            }
        )

        page = await client.user_followers_web_gql_page_result(
            "123",
            end_cursor="previous-web-cursor",
            count=50,
        )

        self.assertEqual([user.pk for user in page.users], ["1", "2"])
        self.assertEqual(page.next_cursor, "opaque-web-relay-cursor")
        self.assertEqual(page.cursor_field, "page_info.end_cursor")
        self.assertIs(page.has_more, True)
        self.assertEqual(page.route, "public_graphql")
        self.assertEqual(page.response_bytes, len(b"web-relay-response"))
        self.assertEqual(page.response_keys, (PUBLIC_WEB_FOLLOWERS_CONNECTION,))
        self.assertEqual(
            page.root_keys,
            ("edges", "page_info", "should_limit_list_of_followers"),
        )
        client.public_doc_id_graphql_request.assert_awaited_once_with(
            PUBLIC_WEB_FOLLOWERS_DOC_ID,
            {
                "after": "previous-web-cursor",
                "before": None,
                "count": 50,
                "first": 50,
                "isFollowerList": True,
                "last": None,
                "query": "",
                "userID": "123",
            },
            retries_count=1,
            friendly_name=PUBLIC_WEB_FOLLOWERS_FRIENDLY_NAME,
            web_headers=True,
            headers={"X-Root-Field-Name": PUBLIC_WEB_FOLLOWERS_CONNECTION},
        )

    async def test_user_followers_web_gql_page_result_serializes_exact_http_request_without_network(self):
        client = Client()
        client.last_response_ts = 0
        client.authorization_data = {"sessionid": "123:session", "ds_user_id": "123"}
        client.private.set_cookies({"csrftoken": "private-csrf-token"})
        client.public.get = AsyncMock(side_effect=AssertionError("web GraphQL must not bootstrap with a GET"))

        response_body = {
            "data": {
                PUBLIC_WEB_FOLLOWERS_CONNECTION: {
                    "edges": [{"node": {"id": "1", "username": "one"}}],
                    "page_info": {
                        "has_next_page": True,
                        "end_cursor": "opaque-web-relay-cursor",
                    },
                    "should_limit_list_of_followers": False,
                }
            }
        }
        response = Mock()
        response.status_code = 200
        response.url = client.GRAPHQL_PUBLIC_WEB_API_URL
        response.content = b'{"data":{"xdt_api__v1__friendships__followers__connection":{}}}'
        response.json.return_value = response_body
        response.raise_for_status.return_value = None
        client.public.post = AsyncMock(return_value=response)

        page = await client.user_followers_web_gql_page_result(
            "456",
            end_cursor="previous-web-cursor",
            count=50,
        )

        self.assertEqual([user.pk for user in page.users], ["1"])
        self.assertEqual(page.next_cursor, "opaque-web-relay-cursor")
        client.public.get.assert_not_awaited()
        client.public.post.assert_awaited_once()
        request = client.public.post.await_args
        self.assertEqual(request.args[0], client.GRAPHQL_PUBLIC_WEB_API_URL)
        self.assertIsNone(request.kwargs["params"])
        self.assertEqual(
            json.loads(request.kwargs["data"]["variables"]),
            {
                "after": "previous-web-cursor",
                "before": None,
                "count": 50,
                "first": 50,
                "isFollowerList": True,
                "last": None,
                "query": "",
                "userID": "456",
            },
        )
        self.assertEqual(request.kwargs["data"]["doc_id"], PUBLIC_WEB_FOLLOWERS_DOC_ID)
        self.assertEqual(request.kwargs["data"]["av"], "123")
        self.assertEqual(request.kwargs["data"]["fb_api_caller_class"], "RelayModern")
        self.assertEqual(
            request.kwargs["data"]["fb_api_req_friendly_name"],
            PUBLIC_WEB_FOLLOWERS_FRIENDLY_NAME,
        )
        headers = request.kwargs["headers"]
        self.assertEqual(headers["X-ASBD-ID"], "359341")
        self.assertEqual(headers["X-IG-App-ID"], "936619743392459")
        self.assertEqual(headers["X-CSRFToken"], "private-csrf-token")
        self.assertEqual(headers["X-FB-Friendly-Name"], PUBLIC_WEB_FOLLOWERS_FRIENDLY_NAME)
        self.assertEqual(headers["X-Root-Field-Name"], PUBLIC_WEB_FOLLOWERS_CONNECTION)
        self.assertEqual(client.public.cookies_dict()["sessionid"], "123:session")
        self.assertEqual(client.public.cookies_dict()["ds_user_id"], "123")
        self.assertNotIn("X-FB-Friendly-Name", client.public.headers)
        self.assertNotIn("X-Root-Field-Name", client.public.headers)

    async def test_user_followers_web_gql_page_result_rejects_untrusted_doc_id_before_network(self):
        client = Client()
        client.public_doc_id_graphql_request = AsyncMock()

        with self.assertRaises(ValueError):
            await client.user_followers_web_gql_page_result("123", doc_id="not-a-doc-id")
        with self.assertRaises(ValueError):
            await client.user_followers_web_gql_page_result(
                "123",
                count=MAX_PUBLIC_GRAPHQL_USER_COUNT + 1,
            )

        client.public_doc_id_graphql_request.assert_not_awaited()

    async def test_user_following_web_gql_page_result_selects_the_following_connection(self):
        client = Client()
        client.public_doc_id_graphql_request = AsyncMock(
            return_value={
                PUBLIC_WEB_FOLLOWING_CONNECTION: {
                    "edges": [{"node": {"id": "7", "username": "seven"}}],
                    "page_info": {"has_next_page": False, "end_cursor": None},
                }
            }
        )

        page = await client.user_following_web_gql_page_result("123", count=24)

        self.assertEqual([user.pk for user in page.users], ["7"])
        self.assertEqual(page.next_cursor, "")
        self.assertIs(page.has_more, False)
        variables = client.public_doc_id_graphql_request.await_args.args[1]
        self.assertIs(variables["isFollowerList"], False)
        self.assertEqual(variables["count"], 24)

    async def test_user_followers_gql_page_result_rejects_invalid_request_before_network(self):
        client = Client()
        client.public_graphql_request = AsyncMock()

        with self.assertRaises(ValueError):
            await client.user_followers_gql_page_result(
                "123",
                count=MAX_PUBLIC_GRAPHQL_USER_COUNT + 1,
            )
        with self.assertRaises(ValueError):
            await client.user_followers_gql_page_result(
                "123",
                query_hash="untrusted-dynamic-value",
            )

        client.public_graphql_request.assert_not_awaited()

    async def test_user_followers_gql_page_result_fails_closed_on_missing_connection(self):
        client = Client()
        client.public_graphql_request = AsyncMock(return_value={"user": {}})

        with self.assertRaises(ClientGraphqlError):
            await client.user_followers_gql_page_result("123")

    async def test_user_following_gql_page_result_uses_following_connection(self):
        client = Client()
        client.public_graphql_request = AsyncMock(
            return_value={
                "user": {
                    "edge_follow": {
                        "edges": [{"node": {"id": "7", "username": "seven"}}],
                        "page_info": {
                            "has_next_page": True,
                            "end_cursor": "following-cursor",
                        },
                    }
                }
            }
        )

        page = await client.user_following_gql_page_result("123", count=24)

        self.assertEqual([user.pk for user in page.users], ["7"])
        self.assertEqual(page.next_cursor, "following-cursor")
        self.assertEqual(page.route, "public_graphql")
        client.public_graphql_request.assert_awaited_once_with(
            {
                "id": "123",
                "include_reel": True,
                "fetch_mutual": False,
                "first": 24,
            },
            query_hash=PUBLIC_FOLLOWING_QUERY_HASH,
            retries_count=1,
        )

    async def test_user_following_v1_page_rejects_oversized_page(self):
        client = Client()

        with self.assertRaises(ValueError):
            await client.user_following_v1_page("123", count=MAX_USER_COUNT + 1)

    async def test_user_followers_v1_chunk_caps_count_to_max_user_count(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_request = AsyncMock(return_value={"users": [], "next_max_id": None})

        await client.user_followers_v1_chunk("123", max_amount=MAX_USER_COUNT + 1)

        client.private_request.assert_awaited_once()
        self.assertEqual(client.private_request.call_args.kwargs["params"]["count"], MAX_USER_COUNT)

    async def test_user_following_v1_chunk_caps_count_to_max_user_count(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_request = AsyncMock(return_value={"users": [], "next_max_id": None})

        await client.user_following_v1_chunk("123", max_amount=MAX_USER_COUNT + 1)

        client.private_request.assert_awaited_once()
        self.assertEqual(client.private_request.call_args.kwargs["params"]["count"], MAX_USER_COUNT)

    async def test_user_followers_v1_chunk_stops_on_repeated_cursor(self):
        client = Client()
        client.user_followers_v1_page = AsyncMock(
            side_effect=[
                ([UserShort(pk="1", username="one")], "repeated-cursor"),
                ([UserShort(pk="2", username="two")], "repeated-cursor"),
            ]
        )

        with self.assertRaisesRegex(ClientError, "Private followers cursor repeated"):
            await client.user_followers_v1_chunk("123")

        self.assertEqual(client.user_followers_v1_page.await_count, 2)

    async def test_user_following_v1_chunk_stops_on_repeated_cursor(self):
        client = Client()
        client.user_following_v1_page = AsyncMock(
            side_effect=[
                ([UserShort(pk="1", username="one")], "repeated-cursor"),
                ([UserShort(pk="2", username="two")], "repeated-cursor"),
            ]
        )

        with self.assertRaisesRegex(ClientError, "Private following cursor repeated"):
            await client.user_following_v1_chunk("123")

        self.assertEqual(client.user_following_v1_page.await_count, 2)

    async def test_iter_user_followers_v1_streams_chunks_and_respects_amount(self):
        client = self._build_private_client()
        users = [
            UserShort(pk="1", username="one"),
            UserShort(pk="2", username="two"),
            UserShort(pk="3", username="three"),
            UserShort(pk="4", username="four"),
        ]
        client.user_followers_v1_chunk = AsyncMock(side_effect=[(users[:2], "cursor-1"), (users[2:], "cursor-2")])

        result = []
        async for user in client.iter_user_followers_v1(
            "123",
            amount=3,
            page_size=2,
            order="date_followed_latest",
        ):
            result.append(user)

        self.assertEqual([user.pk for user in result], ["1", "2", "3"])
        self.assertEqual(
            client.user_followers_v1_chunk.await_args_list[0].kwargs,
            {"max_amount": 2, "max_id": "", "order": "date_followed_latest"},
        )
        self.assertEqual(
            client.user_followers_v1_chunk.await_args_list[1].kwargs,
            {"max_amount": 1, "max_id": "cursor-1", "order": "date_followed_latest"},
        )
        self.assertEqual(client.user_followers_v1_chunk.await_count, 2)

    async def test_iter_user_following_v1_streams_chunks_and_respects_amount(self):
        client = self._build_private_client()
        users = [
            UserShort(pk="1", username="one"),
            UserShort(pk="2", username="two"),
            UserShort(pk="3", username="three"),
        ]
        client.user_following_v1_chunk = AsyncMock(side_effect=[(users[:2], "cursor-1"), (users[2:], "")])

        result = []
        async for user in client.iter_user_following_v1("123", amount=3, page_size=2):
            result.append(user)

        self.assertEqual([user.pk for user in result], ["1", "2", "3"])
        self.assertEqual(
            client.user_following_v1_chunk.await_args_list[0].kwargs,
            {"max_amount": 2, "max_id": ""},
        )
        self.assertEqual(
            client.user_following_v1_chunk.await_args_list[1].kwargs,
            {"max_amount": 1, "max_id": "cursor-1"},
        )
        self.assertEqual(client.user_following_v1_chunk.await_count, 2)

    async def test_user_followers_falls_back_when_private_list_is_limited(self):
        client = Client()
        client.authorization_data = {"sessionid": "sessionid-value", "ds_user_id": "1"}
        client._users_followers = {}
        private_user = Mock(pk="private")
        public_user = Mock(pk="public")

        async def private_lookup(user_id, amount):
            client.last_json = {"should_limit_list_of_followers": True}
            return [private_user]

        client.user_followers_v1 = AsyncMock(side_effect=private_lookup)
        client.user_followers_gql = AsyncMock(return_value=[public_user])

        result = await client.user_followers("123", amount=2, use_cache=False)

        self.assertEqual(list(result.keys()), ["public"])
        client.user_followers_v1.assert_awaited_once_with("123", 2)
        client.user_followers_gql.assert_awaited_once_with("123", 2)

    async def test_user_followers_default_amount_falls_back_when_private_list_is_limited(self):
        client = Client()
        client.authorization_data = {"sessionid": "sessionid-value", "ds_user_id": "1"}
        client._users_followers = {}
        private_user = Mock(pk="private")
        public_user = Mock(pk="public")

        async def private_lookup(user_id, amount):
            client.last_json = {"should_limit_list_of_followers": True}
            return [private_user]

        client.user_followers_v1 = AsyncMock(side_effect=private_lookup)
        client.user_followers_gql = AsyncMock(return_value=[public_user])

        result = await client.user_followers("123", use_cache=False)

        self.assertEqual(list(result.keys()), ["public"])
        client.user_followers_v1.assert_awaited_once_with("123", 0)
        client.user_followers_gql.assert_awaited_once_with("123", 0)

    async def test_user_followers_private_gql_chunk_extracts_followers_payload(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_graphql_followers_list = AsyncMock(
            return_value={
                "data": {
                    "xdt_api__v1__friendships__followers": {
                        "users": [
                            {
                                "pk": "42",
                                "username": "follower",
                                "full_name": "Follower",
                                "profile_pic_url": None,
                            }
                        ],
                        "next_max_id": "next",
                    }
                }
            }
        )

        users, next_max_id = await client.user_followers_private_gql_chunk(
            "123",
            max_amount=1,
            order="date_followed_latest",
        )

        self.assertEqual(next_max_id, "next")
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].pk, "42")
        client.private_graphql_followers_list.assert_awaited_once_with(
            "123",
            "rank-token",
            max_id=None,
            order="date_followed_latest",
            priority="u=3, i",
        )

    async def test_user_followers_private_gql_deduplicates_across_pages(self):
        client = Client()
        client.user_followers_private_gql_chunk = AsyncMock(
            side_effect=[
                (
                    [
                        UserShort(pk="1", username="one"),
                        UserShort(pk="2", username="two"),
                    ],
                    "cursor-1",
                ),
                (
                    [
                        UserShort(pk="2", username="two"),
                        UserShort(pk="3", username="three"),
                    ],
                    "cursor-2",
                ),
            ]
        )

        users = await client.user_followers_private_gql("123", amount=3)

        self.assertEqual([user.pk for user in users], ["1", "2", "3"])
        self.assertEqual(client.user_followers_private_gql_chunk.await_count, 2)
        self.assertEqual(client.user_followers_private_gql_chunk.await_args_list[1].kwargs["max_id"], "cursor-1")

    async def test_user_followers_private_gql_stops_on_repeated_cursor(self):
        client = Client()
        client.user_followers_private_gql_chunk = AsyncMock(
            side_effect=[
                ([UserShort(pk="1", username="one")], "repeated-cursor"),
                ([UserShort(pk="2", username="two")], "repeated-cursor"),
            ]
        )

        with self.assertRaisesRegex(ClientGraphqlError, "Private GraphQL followers cursor repeated"):
            await client.user_followers_private_gql("123")

        self.assertEqual(client.user_followers_private_gql_chunk.await_count, 2)

    async def test_user_followers_private_gql_page_result_accepts_nested_page_info(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_graphql_followers_list = AsyncMock(
            return_value={
                "data": {
                    "xdt_api__v1__friendships__followers": {
                        "edges": [
                            {"node": {"pk": "42", "username": "follower"}},
                        ],
                        "page_info": {
                            "end_cursor": "graphql-cursor",
                            "has_next_page": True,
                        },
                    }
                }
            }
        )

        page = await client.user_followers_private_gql_page_result("123")

        self.assertEqual([user.pk for user in page.users], ["42"])
        self.assertEqual(page.next_cursor, "graphql-cursor")
        self.assertEqual(page.cursor_field, "page_info.end_cursor")
        self.assertIs(page.has_more, True)
        self.assertEqual(page.route, "private_graphql")

    async def test_user_followers_private_gql_page_result_records_safe_limit_signals(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_graphql_followers_list = AsyncMock(
            return_value={
                "data": {
                    "xdt_api__v1__friendships__followers": {
                        "users": [{"pk": "42", "username": "follower"}],
                        "page_size": "50",
                        "big_list": True,
                        "should_limit_list_of_followers": False,
                    }
                }
            }
        )

        page = await client.user_followers_private_gql_page_result(
            "123",
            max_id=50,
            client_doc_id="fresh-doc-id",
            query_profile="pagination_metadata",
        )

        self.assertEqual(page.page_size, 50)
        self.assertIs(page.big_list, True)
        self.assertIs(page.should_limit_list_of_followers, False)
        client.private_graphql_followers_list.assert_awaited_once_with(
            "123",
            "rank-token",
            max_id=50,
            order=None,
            priority="u=3, i",
            client_doc_id="fresh-doc-id",
            query_profile="pagination_metadata",
        )

    async def test_private_graphql_followers_pagination_metadata_profile_requests_metadata(self):
        client = Client()
        captured = {}

        async def fake_request(**kwargs):
            captured.update(kwargs)
            return {"data": {}}

        client.private_graphql_query_request = fake_request

        await client.private_graphql_followers_list(
            "123",
            "rank-token",
            max_id=50,
            order="date_followed_latest",
            query_profile="pagination_metadata",
        )

        variables = captured["variables"]
        self.assertEqual(variables["max_id"], 50)
        self.assertEqual(variables["order"], "date_followed_latest")
        self.assertIs(variables["skip_page_size"], False)
        self.assertIs(variables["skip_has_more"], False)
        self.assertIs(variables["skip_big_list"], False)

    async def test_private_graphql_followers_legacy_sparse_profile_is_exact_and_bounded(self):
        client = Client()
        captured = {}

        async def fake_request(**kwargs):
            captured.update(kwargs)
            return {"data": {}}

        client.private_graphql_query_request = fake_request

        await client.private_graphql_followers_list(
            "123",
            "rank-token",
            max_id=100,
            query_profile="legacy_sparse",
        )

        self.assertEqual(
            captured["variables"],
            {
                "include_unseen_count": False,
                "query": "",
                "include_biography": False,
                "user_id": "123",
                "request_data": {"rank_token": "rank-token", "enableGroups": True},
                "search_surface": "follow_list_page",
                "max_id": 100,
            },
        )

    async def test_private_graphql_followers_rejects_unknown_query_profile(self):
        client = Client()
        client.private_graphql_query_request = AsyncMock(return_value={"data": {}})

        with self.assertRaisesRegex(ValueError, "unsupported follow-list query profile"):
            await client.private_graphql_followers_list(
                "123",
                "rank-token",
                query_profile="unsafe-profile",
            )

        client.private_graphql_query_request.assert_not_awaited()

    async def test_user_followers_private_gql_raises_on_missing_payload(self):
        client = Client()
        client.uuid = "rank-token"
        client.private_graphql_followers_list = AsyncMock(return_value={"data": {}})

        with self.assertRaises(ClientGraphqlError):
            await client.user_followers_private_gql_chunk("123")

    async def test_user_followers_uses_private_first_when_authorized(self):
        client = Client()
        client.authorization_data = {"sessionid": "sessionid-value", "ds_user_id": "1"}
        client._users_followers = {}
        follower = Mock(pk="42")
        client.user_followers_v1 = AsyncMock(return_value=[follower])
        client.user_followers_gql = AsyncMock(
            side_effect=AssertionError("authorized followers lookup should use private first")
        )

        result = await client.user_followers("123", amount=1)

        self.assertEqual(list(result.keys()), ["42"])
        client.user_followers_v1.assert_awaited_once_with("123", 1)
        client.user_followers_gql.assert_not_awaited()

    async def test_user_following_uses_private_first_when_authorized(self):
        client = Client()
        client.authorization_data = {"sessionid": "sessionid-value", "ds_user_id": "1"}
        client._users_following = {}
        following = Mock(pk="43")
        client.user_following_v1 = AsyncMock(return_value=[following])
        client.user_following_gql = AsyncMock(
            side_effect=AssertionError("authorized following lookup should use private first")
        )

        result = await client.user_following("123", amount=1)

        self.assertEqual(list(result.keys()), ["43"])
        client.user_following_v1.assert_awaited_once_with("123", 1)
        client.user_following_gql.assert_not_awaited()

    async def test_user_follow_requests_chunk_fetches_pending_users(self):
        client = self._build_private_client()
        client.private_request = AsyncMock(
            return_value={
                "users": [
                    {
                        "pk": "42",
                        "username": "pending",
                        "full_name": "Pending User",
                        "profile_pic_url": None,
                    }
                ],
                "next_max_id": "next",
            }
        )

        users, next_max_id = await client.user_follow_requests_chunk(max_amount=1)

        self.assertEqual(next_max_id, "next")
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].pk, "42")
        client.private_request.assert_awaited_once_with("friendships/pending/", params={"count": 1})

    async def test_user_follow_requests_chunk_sends_non_empty_max_id(self):
        client = self._build_private_client()
        client.private_request = AsyncMock(return_value={"users": [], "next_max_id": None})

        await client.user_follow_requests_chunk(max_amount=20, max_id="cursor")

        client.private_request.assert_awaited_once_with(
            "friendships/pending/",
            params={"count": 20, "max_id": "cursor"},
        )

    async def test_user_follow_request_approve_posts_action_data_and_returns_status(self):
        client = self._build_private_client()
        client.private_request = AsyncMock(return_value={"friendship_status": {"followed_by": True}})

        result = await client.user_follow_request_approve("42")

        self.assertTrue(result)
        endpoint, data = client.private_request.call_args.args
        self.assertEqual(endpoint, "friendships/approve/42/")
        self.assertEqual(data["user_id"], "42")

    async def test_user_follow_request_decline_posts_action_data_and_returns_status(self):
        client = self._build_private_client()
        client.private_request = AsyncMock(return_value={"friendship_status": {"followed_by": False}})

        result = await client.user_follow_request_decline("42")

        self.assertTrue(result)
        endpoint, data = client.private_request.call_args.args
        self.assertEqual(endpoint, "friendships/ignore/42/")
        self.assertEqual(data["user_id"], "42")

    async def test_user_follow_requests_approve_batches_results(self):
        client = self._build_private_client()
        client.user_follow_request_approve = AsyncMock(side_effect=[True, False])

        result = await client.user_follow_requests_approve(["1", "2"])

        self.assertEqual(result, {"1": True, "2": False})
        client.user_follow_request_approve.assert_has_awaits([unittest.mock.call("1"), unittest.mock.call("2")])

    async def test_user_follow_requests_decline_batches_results(self):
        client = self._build_private_client()
        client.user_follow_request_decline = AsyncMock(side_effect=[False, True])

        result = await client.user_follow_requests_decline(["1", "2"])

        self.assertEqual(result, {"1": False, "2": True})
        client.user_follow_request_decline.assert_has_awaits([unittest.mock.call("1"), unittest.mock.call("2")])

    async def test_user_follow_posts_current_action_context(self):
        client = self._build_action_client()
        client.user_friendship_v1 = AsyncMock(return_value=Mock(following=False, outgoing_request=False))
        client.private_request = AsyncMock(return_value={"friendship_status": {"following": True}})

        self.assertTrue(await client.user_follow("42"))

        endpoint, data = client.private_request.call_args.args
        self.assertEqual(endpoint, "friendships/create/42/")
        self.assertEqual(data["user_id"], "42")
        self.assertEqual(data["_uid"], "1")
        self.assertEqual(data["device_id"], "android-device")
        self.assertEqual(data["radio_type"], "wifi-none")
        self.assertEqual(data["include_follow_friction_check"], "1")
        self.assertEqual(data["container_module"], "profile")

    async def test_user_follow_returns_true_for_pending_private_follow_request(self):
        client = self._build_private_client()
        client.user_friendship_v1 = AsyncMock(return_value=Mock(following=False, outgoing_request=False))
        client.private_request = AsyncMock(
            return_value={"friendship_status": {"following": False, "outgoing_request": True}}
        )

        self.assertTrue(await client.user_follow("42"))

    async def test_user_follow_skips_create_when_friendship_already_following(self):
        client = self._build_private_client()
        client.user_friendship_v1 = AsyncMock(return_value=Mock(following=True, outgoing_request=False))
        client.private_request = AsyncMock(
            side_effect=AssertionError("already-followed users should not be followed again")
        )

        self.assertFalse(await client.user_follow("42"))

        client.user_friendship_v1.assert_awaited_once_with("42")
        client.private_request.assert_not_awaited()

    async def test_user_follow_updates_existing_following_cache_after_success(self):
        client = self._build_private_client()
        client._users_following = {str(client.user_id): {}}
        client.user_friendship_v1 = AsyncMock(return_value=Mock(following=False, outgoing_request=False))
        client.private_request = AsyncMock(return_value={"friendship_status": {"following": True}})

        self.assertTrue(await client.user_follow("42"))
        self.assertFalse(await client.user_follow("42"))

        client.private_request.assert_awaited_once()
        self.assertIn("42", client._users_following[str(client.user_id)])
        self.assertIsInstance(client._users_following[str(client.user_id)]["42"], UserShort)

    async def test_user_unfollow_posts_current_action_context(self):
        client = self._build_action_client()
        client.private_request = AsyncMock(return_value={"friendship_status": {"following": False}})

        self.assertTrue(await client.user_unfollow("42"))

        endpoint, data = client.private_request.call_args.args
        self.assertEqual(endpoint, "friendships/destroy/42/")
        self.assertEqual(data["user_id"], "42")
        self.assertEqual(data["_uid"], "1")
        self.assertEqual(data["device_id"], "android-device")
        self.assertEqual(data["radio_type"], "wifi-none")
        self.assertEqual(data["container_module"], "profile")

    async def test_address_book_link_posts_contacts_payload(self):
        client = Client()
        client.uuid = "uuid"
        client._user_id = "123"
        client.private_request = AsyncMock(return_value={"users": []})
        contacts = [
            {
                "phone_numbers": [{"phone_number": "+15555550123"}],
                "email_addresses": [],
                "first_name": "Test",
                "last_name": "Contact",
            }
        ]

        result = await client.address_book_link(contacts)

        self.assertEqual(result, {"users": []})
        client.private_request.assert_awaited_once_with(
            "address_book/link/",
            data={
                "contacts": json.dumps(contacts, separators=(",", ":")),
                "_uuid": "uuid",
                "_uid": "123",
            },
            params={"include": "extra_display_name,thumbnails"},
        )

    async def test_address_book_link_allows_empty_include(self):
        client = Client()
        client.uuid = "uuid"
        client.private_request = AsyncMock(return_value={"status": "ok"})

        await client.address_book_link([], include="")

        client.private_request.assert_awaited_once_with(
            "address_book/link/",
            data={"contacts": "[]", "_uuid": "uuid"},
            params=None,
        )

    async def test_address_book_link_accepts_typed_contacts_and_include_list(self):
        client = Client()
        client.uuid = "uuid"
        client.private_request = AsyncMock(return_value={"status": "ok"})
        contact = ig_types.AddressBookContact(
            phone_numbers=[ig_types.AddressBookPhone(phone_number="+15555550123")],
            email_addresses=[ig_types.AddressBookEmail(email_address="test@example.com")],
            first_name="Test",
            last_name="Contact",
        )
        expected_contacts = [
            {
                "phone_numbers": [{"phone_number": "+15555550123"}],
                "email_addresses": [{"email_address": "test@example.com"}],
                "first_name": "Test",
                "last_name": "Contact",
            }
        ]

        await client.address_book_link([contact], include=["extra_display_name", "thumbnails"])

        client.private_request.assert_awaited_once_with(
            "address_book/link/",
            data={
                "contacts": json.dumps(expected_contacts, separators=(",", ":")),
                "_uuid": "uuid",
            },
            params={"include": "extra_display_name,thumbnails"},
        )

    async def test_address_book_unlink_posts_uuid(self):
        client = Client()
        client.uuid = "uuid"
        client.private_request = AsyncMock(return_value={"status": "ok"})

        result = await client.address_book_unlink()

        self.assertEqual(result, {"status": "ok"})
        client.private_request.assert_awaited_once_with(
            "address_book/unlink/",
            data={"_uuid": "uuid"},
        )

    async def test_user_report_spam_replays_live_frx_prompt_sequence(self):
        client = Client()
        client.uuid = "uuid"
        responses = [
            {"response": {"context": "context-0"}},
            {"response": {"context": "context-1"}},
            {"response": {"context": "context-2"}},
            {"response": {"context": "context-3", "follow_up_actions": [{"action_type": "block"}]}},
        ]
        client.private_request = AsyncMock(side_effect=responses)

        self.assertTrue(await client.user_report("123"))

        self.assertEqual(client.private_request.await_count, 4)
        calls = client.private_request.await_args_list
        self.assertEqual(calls[0].args, ("reports/get_frx_prompt/",))
        self.assertEqual(
            calls[0].kwargs,
            {
                "data": {
                    "_uuid": "uuid",
                    "container_module": "profile",
                    "entry_point": "1",
                    "frx_prompt_request_type": "1",
                    "is_dark_mode": "false",
                    "location": "2",
                    "nua_action": "",
                    "object_id": "123",
                    "object_type": "5",
                },
                "with_signature": False,
            },
        )
        expected_tags = ["ig_report_account", "ig_its_inappropriate", "ig_spam_v3"]
        for call, expected_context, expected_tag in zip(
            calls[1:], ["context-0", "context-1", "context-2"], expected_tags
        ):
            self.assertEqual(call.args, ("reports/get_frx_prompt/",))
            self.assertEqual(
                call.kwargs,
                {
                    "data": {
                        "_uuid": "uuid",
                        "context": expected_context,
                        "frx_prompt_request_type": "2",
                        "is_dark_mode": "false",
                        "nua_action": "",
                        "selected_tag_types": json.dumps([expected_tag]),
                    },
                    "with_signature": False,
                },
            )

    async def test_user_report_rejects_unknown_reason_before_request(self):
        client = Client()
        client.private_request = AsyncMock(side_effect=AssertionError("unsupported reason should not request"))

        with self.assertRaisesRegex(ValueError, "Unsupported user report reason"):
            await client.user_report("123", reason="harassment")

        client.private_request.assert_not_awaited()

    async def test_user_stream_by_id_v1_parses_first_json_line_from_stream_response(self):
        client = Client()
        client.last_json = {}

        async def private_request(*args, **kwargs):
            client.last_response = Mock(
                text='{"user":{"pk":123,"username":"example"},"status":"ok"}\n{"stream_tail":true}\n',
                status_code=200,
            )
            raise ClientJSONDecodeError("stream response")

        client.private_request = private_request

        result = await client.user_stream_by_id_v1("123")

        self.assertEqual(result["user"]["username"], "example")
        self.assertEqual(result["status"], "ok")

    async def test_user_stream_by_id_v1_raises_user_not_found_for_unparseable_stream(self):
        client = Client()
        client.last_json = {}

        async def private_request(*args, **kwargs):
            client.last_response = Mock(text="not-json\n", status_code=200)
            raise ClientJSONDecodeError("stream response")

        client.private_request = private_request

        with self.assertRaises(UserNotFound):
            await client.user_stream_by_id_v1("123")

    async def test_user_stream_by_id_flat_accepts_top_level_user_payload(self):
        client = Client()
        client.user_stream_by_id_v1 = AsyncMock(return_value={"user": {"pk": "9", "username": "alice"}})

        user = await client.user_stream_by_id_flat("9")

        self.assertEqual(user["pk"], "9")
        self.assertEqual(user["username"], "alice")
