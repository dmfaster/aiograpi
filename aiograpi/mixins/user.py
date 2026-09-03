import json
import logging
import re
from copy import deepcopy
from typing import Any, AsyncIterator, Dict, List, Literal, Optional, Sequence, Tuple, Union

from orjson import JSONDecodeError

from aiograpi.exceptions import (
    ClientError,
    ClientGraphqlError,
    ClientJSONDecodeError,
    ClientLoginRequired,
    ClientNotFoundError,
    ClientStatusFail,
    InvalidTargetUser,
    IsRegulatedC18Error,
    PreLoginRequired,
    PrivateError,
    RelatedProfileRequired,
    UnknownError,
    UserNotFound,
)
from aiograpi.extractors import (
    extract_about_v1,
    extract_guide_v1,
    extract_user_gql,
    extract_user_short,
    extract_user_v1,
)
from aiograpi.mixins.base import ClientMixin
from aiograpi.pagination import (
    UserListPage,
    normalize_collection_bool,
    normalize_collection_cursor,
    normalize_collection_has_more,
    normalize_collection_page_size,
    response_observability,
    safe_mapping_keys,
)
from aiograpi.types import (
    About,
    AddressBookContact,
    Guide,
    Relationship,
    RelationshipShort,
    User,
    UserShort,
)
from aiograpi.utils.iterators import iter_paginated
from aiograpi.utils.serialization import dumps, json_value

MAX_USER_COUNT = 200
MAX_PUBLIC_GRAPHQL_USER_COUNT = 50
DEFAULT_PUBLIC_GRAPHQL_FOLLOWERS_COUNT = 12
DEFAULT_PUBLIC_GRAPHQL_FOLLOWING_COUNT = 24
PUBLIC_FOLLOWERS_QUERY_HASH = "37479f2b8209594dde7facb0d904896a"
PUBLIC_FOLLOWING_QUERY_HASH = "58712303d941c6855d4e888c5f0cd22f"
# Observed in Instagram web server revision 1044017588 on 2026-07-29.
# This registered operation rotates and remains a canary/lab candidate until
# its two-page cursor contract is proven against a bound account and proxy.
PUBLIC_WEB_FOLLOWERS_DOC_ID = "37158170193798755"
PUBLIC_WEB_FOLLOWERS_FRIENDLY_NAME = "usePolarisGetFollowListQuery"
PUBLIC_WEB_FOLLOWERS_CONNECTION = "xdt_api__v1__friendships__followers__connection"
PUBLIC_WEB_FOLLOWING_CONNECTION = "xdt_api__v1__friendships__following__connection"
INFO_FROM_MODULES = ("self_profile", "feed_timeline", "reel_feed_timeline")
FOLLOWERS_ORDERS = ("date_followed_latest", "date_followed_earliest")
USER_WEB_PROFILE_DOC_ID = "26762473490008061"
USER_INFO_V2_DOC_ID = "25980296051578533"
USER_INFO_BY_USERNAME_V2_DOC_ID = "26347858941511777"
# Current anonymous profile operation observed in the public Polaris profile
# route. Instagram rotates registered document IDs; callers must still gate
# this operation behind a bounded canary before relying on it in production.
PUBLIC_WEB_PROFILE_DOC_ID = "18113378221181848"
PUBLIC_WEB_PROFILE_FRIENDLY_NAME = "PolarisProfilePageContentQuery"
FOLLOWERS_LIST_CLIENT_DOC_ID = "28479704797510738576165798526"
# Candidate extracted from the current Android release family. Keep the
# production default pinned above until a bounded account/proxy canary proves
# the rotated document shape and cursor semantics.
FOLLOWERS_LIST_CURRENT_CLIENT_DOC_ID = "284797047915973598462248516468"
FOLLOWING_LIST_CLIENT_DOC_ID = "161046392817718486717479294775"
ADDRESS_BOOK_DEFAULT_INCLUDE = ("extra_display_name", "thumbnails")
USER_REPORT_REASONS = {"spam": ("ig_report_account", "ig_its_inappropriate", "ig_spam_v3")}

logger = logging.getLogger(__name__)

INFO_FROM_MODULE = Literal["self_profile", "feed_timeline", "reel_feed_timeline"]
FOLLOWERS_ORDER = Literal["date_followed_latest", "date_followed_earliest"]
FOLLOW_LIST_QUERY_PROFILE = Literal["canonical", "pagination_metadata", "legacy_sparse"]
USER_REPORT_REASON = Literal["spam"]
UserBlockSurface = Literal["profile", "direct_thread_info"]


def _followers_list_variables(
    user_id: str,
    rank_token: str,
    *,
    max_id: Optional[Union[str, int]],
    order: Optional[FOLLOWERS_ORDER],
    query_profile: FOLLOW_LIST_QUERY_PROFILE,
) -> dict:
    if query_profile not in {"canonical", "pagination_metadata", "legacy_sparse"}:
        raise ValueError("unsupported follow-list query profile")
    request_data = {
        "rank_token": rank_token,
        "enableGroups": True,
    }
    if query_profile == "legacy_sparse":
        variables = {
            "include_unseen_count": False,
            "query": "",
            "include_biography": False,
            "user_id": str(user_id),
            "request_data": request_data,
            "search_surface": "follow_list_page",
        }
    else:
        include_pagination_metadata = query_profile == "pagination_metadata"
        variables = {
            "user_id": str(user_id),
            "skip_suggested_users": True,
            "skip_more_groups_available": True,
            "skip_friendship_followers_fields": True,
            "request_data": request_data,
            "skip_page_size": not include_pagination_metadata,
            "skip_pending_admins": True,
            "skip_has_more": not include_pagination_metadata,
            "search_surface": "follow_list_page",
            "query": "",
            "skip_big_list": not include_pagination_metadata,
            "include_unseen_count": True,
        }
    if max_id is not None:
        variables["max_id"] = max_id
    if order is not None:
        variables["order"] = order
    return variables


def _following_list_variables(
    user_id: str,
    rank_token: str,
    *,
    max_id: Optional[Union[str, int]],
    order: Optional[FOLLOWERS_ORDER],
    query_profile: FOLLOW_LIST_QUERY_PROFILE,
    skip_preview_hashtags: bool,
    skip_hashtag_count: bool,
) -> dict:
    if query_profile not in {"canonical", "pagination_metadata", "legacy_sparse"}:
        raise ValueError("unsupported follow-list query profile")
    request_data = {
        "search_surface": "follow_list_page",
        "rank_token": rank_token,
        "includes_hashtags": True,
    }
    if query_profile == "legacy_sparse":
        variables = {
            "include_unseen_count": False,
            "enable_groups": True,
            "user_id": str(user_id),
            "request_data": request_data,
            "include_biography": False,
            "query": "",
        }
    else:
        include_pagination_metadata = query_profile == "pagination_metadata"
        variables = {
            "user_id": str(user_id),
            "skip_use_clickable_see_more": True,
            "skip_preview_hashtags": skip_preview_hashtags,
            "skip_should_limit_list_of_followers": True,
            "skip_pending_admins": True,
            "skip_more_groups_available": True,
            "skip_friendship_followers_fields": False,
            "request_data": request_data,
            "skip_page_size": not include_pagination_metadata,
            "skip_friend_requests": True,
            "skip_big_list": not include_pagination_metadata,
            "query": "",
            "include_profile_update_info": True,
            "skip_suggested_users": True,
            "include_unseen_count": True,
            "skip_has_more": not include_pagination_metadata,
            "enable_groups": True,
            "skip_hashtag_count": skip_hashtag_count,
        }
    if max_id is not None:
        variables["max_id"] = max_id
    if order is not None:
        variables["order"] = order
    return variables


class UserMixin(ClientMixin):
    """
    Helpers to manage user
    """

    _users_cache: Dict[str, User]  # user_pk -> User
    _userhorts_cache: Dict[str, UserShort]  # user_pk -> UserShort
    _usernames_cache: Dict[str, str]  # username -> user_pk
    _users_following: Dict[Any, Any]  # user_pk -> dict(user_pk -> "short user object")
    _users_followers: Dict[Any, Any]  # user_pk -> dict(user_pk -> "short user object")

    @staticmethod
    def _normalize_username(username: str) -> str:
        return str(username).strip().lstrip("@").strip().lower()

    async def _user_info_by_username_public(self, username: str) -> User:
        try:
            return await self.user_info_by_username_gql(username)
        except ClientLoginRequired as e:
            if not self.inject_sessionid_to_public():
                raise e
            return await self.user_info_by_username_gql(username)

    async def _user_info_public(self, user_id: str) -> User:
        try:
            return await self.user_info_gql(user_id)
        except ClientLoginRequired as e:
            if not self.inject_sessionid_to_public():
                raise e
            return await self.user_info_gql(user_id)

    async def user_id_from_username(self, username: str) -> str:
        """
        Get full media id

        Parameters
        ----------
        username: str
            Username for an Instagram account

        Returns
        -------
        str
            User PK

        Example
        -------
        'example' -> 1903424587
        """
        username = self._normalize_username(username)
        user = await self.user_info_by_username(username)
        return str(user.pk)

    async def user_id_from_username_v1_once(self, username: str) -> str:
        """Resolve one username with exactly one private API attempt.

        This primitive is intended for durable workers that own retry and
        checkpoint policy outside aiograpi. It never falls back to a public
        endpoint and disables the private request layer's transient retry.
        """
        username = self._normalize_username(username)
        try:
            result = await self.private_request(
                f"users/{username}/usernameinfo/",
                retry_transient=False,
                retry_without_cursor=False,
            )
        except ClientNotFoundError as e:
            raise UserNotFound(e, username=username, **self.last_json)
        user = result.get("user") or {}
        user_id = str(user.get("pk") or user.get("id") or "").strip()
        if not user_id:
            raise UserNotFound("User not found", username=username, **self.last_json)
        return user_id

    async def user_short_gql(self, user_id: str) -> UserShort:
        """
        Get full media id

        Parameters
        ----------
        user_id: str
            User ID

        Returns
        -------
        UserShort
            An object of UserShort type
        """
        return extract_user_short(await self.user_web_profile_info_gql(user_id))

    async def user_web_profile_info_gql(self, user_id: str) -> dict:
        """
        Fetch a user profile via the public-host PolarisProfilePageContentQuery.

        ``POST /graphql/query/`` with ``doc_id="26762473490008061"`` —
        the modern web-profile GraphQL surface that replaced the old
        ``query_hash`` profile lookups. Requires a logged-in
        ``sessionid`` (the doc_id rejects anonymous callers).

        Used as the canonical GraphQL fallback for
        :meth:`user_short_gql` when the legacy ``query_hash`` path
        fails.

        Parameters
        ----------
        user_id: str
            Target user pk.

        Returns
        -------
        dict
            The inner ``data["user"]`` block from the GraphQL
            response (already unwrapped).

        Raises
        ------
        ClientLoginRequired
            ``sessionid`` is not available to inject.
        UserNotFound
            Response contained no ``user`` block.
        """
        user_id = str(user_id)
        if not self.inject_sessionid_to_public():
            raise ClientLoginRequired("Session is required for web profile GraphQL")
        variables = {
            "enable_integrity_filters": True,
            "id": user_id,
            "render_surface": "PROFILE",
            "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider": True,
            "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider": False,
        }
        data = await self.public_doc_id_graphql_request(
            USER_WEB_PROFILE_DOC_ID,
            variables,
            referer=f"https://www.instagram.com/{user_id}/",
            headers={"X-FB-Friendly-Name": "PolarisProfilePageContentQuery"},
        )
        if not data or not data.get("user"):
            raise UserNotFound(user_id=user_id, **(data or {}))
        return data["user"]

    async def username_from_user_id_gql(self, user_id: str) -> str:
        """
        Get username from user id

        Parameters
        ----------
        user_id: str
            User ID

        Returns
        -------
        str
            User name

        Example
        -------
        1903424587 -> 'example'
        """
        return (await self.user_short_gql(user_id)).username

    async def username_from_user_id(self, user_id: str) -> str:
        """
        Get username from user id

        Parameters
        ----------
        user_id: str
            User ID

        Returns
        -------
        str
            User name

        Example
        -------
        1903424587 -> 'example'
        """
        user_id = str(user_id)
        if self._has_private_auth():
            try:
                return (await self.user_info_v1(user_id)).username
            except ClientError:
                return await self.username_from_user_id_gql(user_id)
        try:
            return await self.username_from_user_id_gql(user_id)
        except ClientError:
            return (await self.user_info_v1(user_id)).username

    async def user_info_by_username_gql(self, username: str) -> User:
        """
        Get user object from user name

        Parameters
        ----------
        username: str
            User name of an instagram account

        Returns
        -------
        User
            An object of User type
        """
        username = self._normalize_username(username)
        temporary_public_headers = {
            "Host": "www.instagram.com",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Ch-Prefers-Color-Scheme": "dark",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "X-Ig-App-Id": "936619743392459",
            "Sec-Ch-Ua-Model": '""',
            "Sec-Ch-Ua-Mobile": "?0",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.6261.112 Safari/537.36"
            ),
            "Accept": "*/*",
            "X-Asbd-Id": "129477",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://www.instagram.com/",
            "Accept-Language": "en-US,en;q=0.9",
            "Priority": "u=1, i",
        }
        return extract_user_gql(
            json.loads(
                await self.public_request(
                    f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                    headers=temporary_public_headers,
                )
            )["data"]["user"]
        )

    def _inject_sessionid_for_v2_gql(self) -> None:
        """The new doc_id endpoints require logged-in cookies. Bridge the
        private session's sessionid into the public session so
        public_doc_id_graphql_request carries it."""
        try:
            self.inject_sessionid_to_public()
        except Exception:  # nosec B110 - anonymous caller; IG will 403 if auth needed
            pass

    async def user_info_v2_gql(self, user_id: str) -> User:
        """
        Get user object via the new PolarisProfilePageContentQuery doc_id.

        IG migrated logged-in profile fetches off ``api/v1/users/web_profile_info/``
        to a doc_id-based GraphQL endpoint. This method posts to that endpoint
        and normalizes the response into the same legacy shape that
        :func:`extract_user_v1` understands.

        Use this when ``user_info_by_username_gql`` starts returning
        unauthorized / empty for logged-in callers.

        Parameters
        ----------
        user_id: str
            Numeric user id ("pk").

        Returns
        -------
        User
            An object of User type.
        """
        variables = {
            "id": str(user_id),
            "render_surface": "PROFILE",
            # Relay provider flags carried over from PolarisProfilePageContentQuery.
            "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider": True,
            "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider": False,
        }
        self._inject_sessionid_for_v2_gql()
        data = await self.public_doc_id_graphql_request(USER_INFO_V2_DOC_ID, variables)
        user_data = (data or {}).get("user")
        if user_data is None:
            raise UserNotFound("User not found", user_id=user_id)
        return extract_user_v1(self._normalize_polaris_profile(user_data))

    async def user_info_by_id_public_relay(
        self,
        user_id: str,
        username: str,
        *,
        expected_request_count: Optional[int] = None,
    ) -> User:
        """Fetch one full public profile through the anonymous web Relay surface.

        The numeric id is the stable query identity and the username is used
        only to bootstrap Instagram's current anonymous profile-page context.
        The caller owns retries and must account for two HTTP requests on a
        cold public session and one request while that context remains warm.
        """

        normalized_user_id = str(user_id or "").strip()
        normalized_username = self._normalize_username(username)
        if not normalized_user_id.isdigit():
            raise ValueError("user_id must be numeric")
        if not normalized_username or not re.fullmatch(r"[a-z0-9._]{1,30}", normalized_username):
            raise ValueError("username contains unsupported characters")
        variables = {
            "id": normalized_user_id,
            "enable_integrity_filters": True,
            "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
        }
        data = await self.public_web_relay_request(
            PUBLIC_WEB_PROFILE_DOC_ID,
            variables,
            referer=f"https://www.instagram.com/{normalized_username}/",
            friendly_name=PUBLIC_WEB_PROFILE_FRIENDLY_NAME,
            retries_count=1,
            expected_request_count=expected_request_count,
        )
        user_data = (data or {}).get("user")
        if not isinstance(user_data, dict) or not user_data:
            raise UserNotFound("User not found", user_id=normalized_user_id, username=normalized_username)
        return extract_user_v1(self._normalize_polaris_profile(user_data))

    async def user_info_by_username_v2_gql(self, username: str) -> User:
        """
        Get user object via the new doc_id-based GraphQL endpoints.

        Two-step: first resolve username → user_id via the FB search query
        (doc_id 26347858941511777), then fetch the profile via
        :meth:`user_info_v2_gql`. Provides a logged-in-friendly alternative
        to :meth:`user_info_by_username_gql` (which uses the increasingly
        flaky ``api/v1/users/web_profile_info/`` endpoint).

        Parameters
        ----------
        username: str
            User name of an instagram account.

        Returns
        -------
        User
            An object of User type.
        """
        username = self._normalize_username(username)
        self._inject_sessionid_for_v2_gql()
        data = await self.public_doc_id_graphql_request(
            USER_INFO_BY_USERNAME_V2_DOC_ID, {"hasQuery": True, "query": username}
        )
        # Defend against `{"xdt_api__v1__fbsearch__non_profiled_serp": null}` —
        # `.get(key, {})` returns the default ONLY when key is absent;
        # if the key is present with value `None`, the chained `.get` would
        # crash with AttributeError. Promote None → {} explicitly.
        users = ((data or {}).get("xdt_api__v1__fbsearch__non_profiled_serp") or {}).get("users") or []
        for user in users:
            if (user.get("username") or "").lower() == username:
                return await self.user_info_v2_gql(user.get("pk") or user.get("id"))
        raise UserNotFound("User not found", username=username)

    @staticmethod
    def _normalize_polaris_profile(user_data: dict) -> dict:
        """Map PolarisProfilePageContentQuery fields onto the legacy v1 shape
        understood by :func:`extract_user_v1`."""
        normalized = dict(user_data)
        if "pk" not in normalized and "id" in normalized:
            normalized["pk"] = normalized["id"]
        if "is_business" not in normalized and "is_business_account" in normalized:
            normalized["is_business"] = normalized["is_business_account"]
        if "category" not in normalized and "category_name" in normalized:
            normalized["category"] = normalized["category_name"]
        # Logged-out Relay intentionally redacts a small number of counters as
        # null. aiograpi's User contract is numeric; zero preserves the
        # existing "not supplied" fallback used by public profile extraction
        # and downstream merges retain any larger previously observed value.
        for field in ("media_count", "follower_count", "following_count"):
            if normalized.get(field) is None:
                normalized[field] = 0
        # PolarisProfilePageContentQuery puts viewer-relationship flags
        # under friendship_status; aiograpi User doesn't track them, but
        # flatten anyway in case future fields land.
        friendship = normalized.get("friendship_status") or {}
        normalized.setdefault("followed_by_viewer", friendship.get("following", False))
        normalized.setdefault("follows_viewer", friendship.get("followed_by", False))
        return normalized

    async def user_info_by_username_v1(self, username: str) -> User:
        """
        Get user object from user name

        Parameters
        ----------
        username: str
            User name of an instagram account

        Returns
        -------
        User
            An object of User type
        """
        username = self._normalize_username(username)
        try:
            result = await self.private_request(f"users/{username}/usernameinfo/")
        except ClientNotFoundError as e:
            raise UserNotFound(e, username=username, **self.last_json)
        except ClientError as e:
            if "User not found" in str(e):
                raise UserNotFound(e, username=username, **self.last_json)
            if isinstance(e, ClientStatusFail):
                raise IsRegulatedC18Error(username=username, **self.last_json)
            raise e
        if user := result.get("user"):
            return extract_user_v1(user)
        raise UserNotFound("User not found", username=username, **self.last_json)

    async def user_info_by_username(self, username: str) -> User:
        """
        Get user object from username

        Parameters
        ----------
        username: str
            User name of an instagram account

        Returns
        -------
        User
            An object of User type
        """
        username = self._normalize_username(username)
        if username not in self._usernames_cache:
            if self._has_private_auth():
                try:
                    user = await self.user_info_by_username_v1(username)
                except Exception as e:
                    if not isinstance(e, ClientError):
                        self.logger.exception(e)
                    user = await self._user_info_by_username_public(username)
            else:
                try:
                    user = await self._user_info_by_username_public(username)
                except Exception as e:
                    if not isinstance(e, ClientError):
                        self.logger.exception(e)
                    user = await self.user_info_by_username_v1(username)
            self._users_cache[str(user.pk)] = user
            self._usernames_cache[user.username] = str(user.pk)
        return await self.user_info(self._usernames_cache[username])

    async def user_info_gql(self, user_id: str) -> User:
        """
        Get user object from user id

        Parameters
        ----------
        user_id: str
            User id of an instagram account

        Returns
        -------
        User
            An object of User type
        """
        user_id = str(user_id)
        try:
            # GraphQL haven't method to receive user by id
            return await self.user_info_by_username_gql(await self.username_from_user_id_gql(user_id))
        except JSONDecodeError as e:
            raise ClientJSONDecodeError(e, user_id=user_id)

    async def user_info_v1(
        self,
        user_id: str,
        from_module: INFO_FROM_MODULE = "self_profile",
        is_app_start: bool = False,
    ) -> User:
        """
        Get user object from user id

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        from_module: str
            Which module triggered request: self_profile, feed_timeline,
            reel_feed_timeline. Default: self_profile
        is_app_start: bool
            Boolean value specifying if profile is being retrieved on app launch

        Returns
        -------
        User
            An object of User type
        """
        user_id = str(user_id)
        try:
            params = {
                "is_prefetch": "false",
                "entry_point": "self_profile",
                "from_module": from_module,
                "is_app_start": is_app_start,
            }
            assert from_module in INFO_FROM_MODULES, f'Unsupported send_attribute="{from_module}" {INFO_FROM_MODULES}'
            if from_module != "self_profile":
                params["entry_point"] = "profile"

            result = await self.private_request(f"users/{user_id}/info/", params=params)
        except ClientNotFoundError as e:
            raise UserNotFound(e, user_id=user_id, **self.last_json)
        except ClientError as e:
            if "User not found" in str(e):
                raise UserNotFound(e, user_id=user_id, **self.last_json)
            raise e
        if user := result.get("user"):
            return extract_user_v1(user)
        raise UserNotFound("User not found", user_id=user_id, **self.last_json)

    async def user_about_v1(self, user_id: str) -> About:
        """
        Get about info from user id

        Parameters
        ----------
        user_id: str
            User id of an instagram account

        Returns
        -------
        About
            An object of About type
        """
        user_id = str(user_id)
        bk = dumps({"bloks_version": self.bloks_versioning_id, "styles_id": "instagram"})
        data = {
            "referer_type": "ProfileMore",
            "target_user_id": user_id,
            "bk_client_context": bk,
            "bloks_versioning_id": self.bloks_versioning_id,
        }
        try:
            await self.bloks_action("com.instagram.interactions.about_this_account", data)
        except ClientNotFoundError as e:
            raise UserNotFound(e, user_id=user_id, **self.last_json)
        except ClientError as e:
            if "User not found" in str(e):
                raise UserNotFound(e, user_id=user_id, **self.last_json)
            raise e
        return extract_about_v1(self.last_json)

    async def user_info(self, user_id: str) -> User:
        """
        Get user object from user id

        Parameters
        ----------
        user_id: str
            User id of an instagram account

        Returns
        -------
        User
            An object of User type
        """
        user_id = str(user_id)
        if user_id not in self._users_cache:
            if self._has_private_auth():
                try:
                    user = await self.user_info_v1(user_id)
                except Exception as e:
                    if not isinstance(e, ClientError):
                        self.logger.exception(e)
                    user = await self._user_info_public(user_id)
            else:
                try:
                    user = await self._user_info_public(user_id)
                except Exception as e:
                    if not isinstance(e, ClientError):
                        self.logger.exception(e)
                    user = await self.user_info_v1(user_id)
            self._users_cache[user_id] = user
            self._usernames_cache[user.username] = str(user.pk)
        return deepcopy(self._users_cache[user_id])

    async def new_feed_exist(self) -> bool:
        """
        Returns bool
        -------
        Check if new feed exist
        -------
        True if new feed exist ,
        After Login or load Settings always return False
        """
        results = await self.private_request("feed/new_feed_posts_exist/")
        return results.get("new_feed_posts_exist", False)

    async def user_friendships_v1(self, user_ids: List[str]) -> List[RelationshipShort]:
        """
        Get user friendship status

        Parameters
        ----------
        user_ids: List[str]
            List of user ID of an instagram account

        Returns
        -------
        List[RelationshipShort]
           List of RelationshipShorts with requested user_ids
        """
        user_ids_str = ",".join(user_ids)
        result = await self.private_request(
            "friendships/show_many/",
            data={"user_ids": user_ids_str, "_uuid": self.uuid},
            with_signature=False,
        )
        assert result.get("status", "") == "ok"

        relationships = []
        for user_id, status in result.get("friendship_statuses", {}).items():
            relationships.append(RelationshipShort(user_id=user_id, **status))

        return relationships

    async def user_friendship_v1(self, user_id: str) -> Relationship:
        """
        Get user friendship status

        Parameters
        ----------
        user_id: str
            User id of an instagram account

        Returns
        -------
        Relationship
            An object of Relationship type
        """

        try:
            params = {
                "is_external_deeplink_profile_view": "false",
            }
            result = await self.private_request(f"friendships/show/{user_id}/", params=params)
            assert result.get("status", "") == "ok"

            return Relationship(user_id=user_id, **result)
        except ClientError as e:
            self.logger.exception(e)
            return None

    async def search_users_v1(self, query: str, count: int) -> List[UserShort]:
        """
        Search users by a query (Private Mobile API)
        Parameters
        ----------
        query: str
            Query to search
        count: int
            The count of search results
        Returns
        -------
        List[UserShort]
            List of users
        """
        results = await self.private_request("users/search/", params={"query": query, "count": count})
        users = results.get("users", [])
        return [extract_user_short(user) for user in users]

    async def search_users(self, query: str, count: int = 50) -> List[UserShort]:
        """
        Search users by a query
        Parameters
        ----------
        query: str
            Query string to search
        count: int
            The count of search results
        Returns
        -------
        List[UserShort]
            List of User short object
        """
        return await self.search_users_v1(query, count)

    async def search_followers_v1(self, user_id: str, query: str) -> List[UserShort]:
        """
        Search users by followers (Private Mobile API)

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        query: str
            Query to search

        Returns
        -------
        List[UserShort]
            List of users
        """
        results = await self.private_request(
            f"friendships/{user_id}/followers/",
            params={
                "search_surface": "follow_list_page",
                "query": query,
                "enable_groups": "true",
            },
        )
        users = results.get("users", [])
        return [extract_user_short(user) for user in users]

    async def search_followers(self, user_id: str, query: str) -> List[UserShort]:
        """
        Search by followers

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        query: str
            Query string

        Returns
        -------
        List[UserShort]
            List of User short object
        """
        return await self.search_followers_v1(user_id, query)

    async def search_following_v1(self, user_id: str, query: str) -> List[UserShort]:
        """
        Search following users (Private Mobile API)

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        query: str
            Query to search

        Returns
        -------
        List[UserShort]
            List of users
        """
        results = await self.private_request(
            f"friendships/{user_id}/following/",
            params={
                "includes_hashtags": "false",
                "search_surface": "follow_list_page",
                "query": query,
                "enable_groups": "true",
            },
        )
        users = results.get("users", [])
        return [extract_user_short(user) for user in users]

    async def search_following(self, user_id: str, query: str) -> List[UserShort]:
        """
        Search by following

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        query: str
            Query string

        Returns
        -------
        List[UserShort]
            List of User short object
        """
        return await self.search_following_v1(user_id, query)

    async def user_following_gql_chunk(
        self, user_id: str, max_amount: int = 0, end_cursor: str = None
    ) -> Tuple[List[UserShort], str]:
        """
        Get user's following information by Public Graphql API and end_cursor

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        max_amount: int, optional
            Maximum number of users to return, default is 0 - Inf
        end_cursor: str, optional
            The cursor from which it is worth continuing
            to receive the list of following

        Returns
        -------
        Tuple[List[UserShort], str]
            List of objects of User type with cursor
        """
        users: List[UserShort] = []
        seen_cursors = {str(end_cursor or "")}
        while True:
            remaining = max_amount - len(users) if max_amount else DEFAULT_PUBLIC_GRAPHQL_FOLLOWING_COUNT
            page = await self.user_following_gql_page_result(
                user_id,
                end_cursor=end_cursor or "",
                count=min(DEFAULT_PUBLIC_GRAPHQL_FOLLOWING_COUNT, remaining),
            )
            users.extend(page.users)
            next_cursor = page.next_cursor
            if not next_cursor:
                end_cursor = ""
                break
            if max_amount and len(users) >= max_amount:
                end_cursor = next_cursor
                break
            if next_cursor in seen_cursors:
                raise ClientGraphqlError("Public GraphQL following cursor repeated")
            seen_cursors.add(next_cursor)
            end_cursor = next_cursor
        return users, str(end_cursor or "")

    async def user_following_gql_page_result(
        self,
        user_id: str,
        *,
        end_cursor: str = "",
        count: int = DEFAULT_PUBLIC_GRAPHQL_FOLLOWING_COUNT,
        query_hash: str = PUBLIC_FOLLOWING_QUERY_HASH,
    ) -> UserListPage:
        """Fetch exactly one public Relay GraphQL following page."""
        return await self._user_public_gql_page_result(
            user_id,
            connection_field="edge_follow",
            end_cursor=end_cursor,
            count=count,
            query_hash=query_hash,
        )

    async def user_following_gql(self, user_id: str, amount: int = 0) -> List[UserShort]:
        """
        Get user's following users information by Public Graphql API

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of media to return, default is 0 - Inf

        Returns
        -------
        List[UserShort]
            List of objects of User type
        """
        users, _ = await self.user_following_gql_chunk(str(user_id), amount)
        if amount:
            users = users[:amount]
        return users

    async def user_following_v1_chunk(
        self, user_id: str, max_amount: int = 0, max_id: str = ""
    ) -> Tuple[List[UserShort], str]:
        """
        Get user's following users information by Private Mobile API and max_id (cursor)

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        max_amount: int, optional
            Maximum number of users to return, default is 0 - Inf
        max_id: str, optional
            Max ID, default value is empty String

        Returns
        -------
        Tuple[List[UserShort], str]
            Tuple of List of users and max_id
        """
        unique_set = set()
        seen_cursors = {str(max_id or "")}
        users: List[UserShort] = []
        while True:
            page_amount = MAX_USER_COUNT
            if max_amount:
                page_amount = min(max_amount - len(users), MAX_USER_COUNT)
            page, max_id = await self.user_following_v1_page(
                user_id,
                max_id=max_id,
                count=page_amount,
            )
            for user in page:
                if user.pk in unique_set:
                    continue
                unique_set.add(user.pk)
                users.append(user)
            if not max_id or (max_amount and len(users) >= max_amount):
                break
            normalized_cursor = str(max_id)
            if normalized_cursor in seen_cursors:
                raise ClientError("Private following cursor repeated")
            seen_cursors.add(normalized_cursor)
        return users, max_id

    async def user_following_v1_page(
        self,
        user_id: str,
        *,
        max_id: str = "",
        count: int = MAX_USER_COUNT,
    ) -> Tuple[List[UserShort], str]:
        """Fetch exactly one Private Mobile API page of followed users.

        Unlike :meth:`user_following_v1_chunk`, this method never follows the
        returned cursor. It is intended for durable workers that checkpoint
        every upstream response before requesting another page.
        """
        page = await self.user_following_v1_page_result(
            user_id,
            max_id=max_id,
            count=count,
        )
        return page.users, page.next_cursor

    async def user_following_v1_page_result(
        self,
        user_id: str,
        *,
        max_id: str = "",
        count: int = MAX_USER_COUNT,
    ) -> UserListPage:
        """Fetch one followed-users page with lossless pagination metadata."""
        if not 1 <= count <= MAX_USER_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_USER_COUNT}")
        params = {
            "count": count,
            "rank_token": self.rank_token,
            "search_surface": "follow_list_page",
            "query": "",
            "enable_groups": "true",
        }
        if max_id:
            params["max_id"] = max_id
        result = await self.private_request(
            f"friendships/{user_id}/following/",
            params=params,
            retry_transient=False,
            retry_without_cursor=False,
        )
        raw_users = result.get("users") if isinstance(result.get("users"), list) else []
        users = [extract_user_short(user) for user in raw_users]
        next_cursor, cursor_field = normalize_collection_cursor(result)
        http_status, response_bytes = response_observability(self.last_response)
        return UserListPage(
            users=users,
            next_cursor=next_cursor,
            cursor_field=cursor_field,
            has_more=normalize_collection_has_more(result, next_cursor=next_cursor),
            route="private_v1",
            response_keys=safe_mapping_keys(result),
            root_keys=safe_mapping_keys(result),
            raw_user_count=len(raw_users),
            http_status=http_status,
            response_bytes=response_bytes,
            page_size=normalize_collection_page_size(result),
            big_list=normalize_collection_bool(result, "big_list"),
            should_limit_list_of_followers=normalize_collection_bool(
                result,
                "should_limit_list_of_followers",
            ),
        )

    async def user_following_v1(self, user_id: str, amount: int = 0) -> List[UserShort]:
        """
        Get user's following users information by Private Mobile API

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of media to return, default is 0

        Returns
        -------
        List[UserShort]
            List of objects of User type
        """
        users, _ = await self.user_following_v1_chunk(str(user_id), amount)
        if amount:
            users = users[:amount]
        return users

    def iter_user_following_v1(
        self,
        user_id: str,
        amount: int = 0,
        page_size: int = MAX_USER_COUNT,
    ) -> AsyncIterator[UserShort]:
        """
        Iterate over user's following users by Private Mobile API.

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of users to yield, default is 0 - Inf
        page_size: int, optional
            Maximum number of users to fetch per page, default is 200

        Returns
        -------
        AsyncIterator[UserShort]
            Async iterator of UserShort objects
        """
        user_id = str(user_id)

        async def fetch_page(max_id: Optional[str], max_amount: int) -> Tuple[List[UserShort], Optional[str]]:
            return await self.user_following_v1_chunk(user_id, max_amount=max_amount, max_id=max_id or "")

        return iter_paginated(fetch_page, amount=amount, page_size=page_size, initial_cursor="")

    async def user_following(self, user_id: str, amount: int = 0, use_cache: bool = True) -> Dict[str, UserShort]:
        """
        Get user's followers information

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of media to return, default is 0
        use_cache: bool, optional
            Whether or not to use information from cache, default value is True

        Returns
        -------
        Dict[str, UserShort]
            Dict of user_id and User object
        """
        user_id = str(user_id)
        users: Any = self._users_following.get(user_id, {})
        if not use_cache or not users or (amount and len(users) < amount):
            if self._has_private_auth():
                try:
                    users = await self.user_following_v1(user_id, amount)
                except Exception as e:
                    if not isinstance(e, ClientError):
                        self.logger.exception(e)
                    users = await self.user_following_gql(user_id, amount)
            else:
                try:
                    users = await self.user_following_gql(user_id, amount)
                except Exception as e:
                    if not isinstance(e, ClientError):
                        self.logger.exception(e)
                    users = await self.user_following_v1(user_id, amount)
            self._users_following[user_id] = {user.pk: user for user in users}
        following = self._users_following[user_id]
        if amount and len(following) > amount:
            following = dict(list(following.items())[:amount])
        return following

    async def user_followers_gql_chunk(
        self, user_id: str, max_amount: int = 0, end_cursor: str = None
    ) -> Tuple[List[UserShort], str]:
        """
        Get user's followers information by Public Graphql API and end_cursor

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        max_amount: int, optional
            Maximum number of users to return, default is 0 - Inf
        end_cursor: str, optional
            The cursor from which it is worth continuing
            to receive the list of followers

        Returns
        -------
        Tuple[List[UserShort], str]
            List of objects of User type with cursor
        """
        users: List[UserShort] = []
        seen_cursors = {str(end_cursor or "")}
        while True:
            remaining = max_amount - len(users) if max_amount else DEFAULT_PUBLIC_GRAPHQL_FOLLOWERS_COUNT
            page = await self.user_followers_gql_page_result(
                user_id,
                end_cursor=end_cursor or "",
                count=min(DEFAULT_PUBLIC_GRAPHQL_FOLLOWERS_COUNT, remaining),
            )
            users.extend(page.users)
            next_cursor = page.next_cursor
            if not next_cursor:
                end_cursor = ""
                break
            if max_amount and len(users) >= max_amount:
                end_cursor = next_cursor
                break
            if next_cursor in seen_cursors:
                raise ClientGraphqlError("Public GraphQL followers cursor repeated")
            seen_cursors.add(next_cursor)
            end_cursor = next_cursor
        return users, str(end_cursor or "")

    async def user_followers_gql_page_result(
        self,
        user_id: str,
        *,
        end_cursor: str = "",
        count: int = DEFAULT_PUBLIC_GRAPHQL_FOLLOWERS_COUNT,
        query_hash: str = PUBLIC_FOLLOWERS_QUERY_HASH,
    ) -> UserListPage:
        """Fetch exactly one public Relay GraphQL followers page.

        The opaque ``end_cursor`` is returned without being consumed so a
        durable worker can commit the users and checkpoint atomically. This
        method always performs one provider request and never logs the cursor.
        """
        return await self._user_public_gql_page_result(
            user_id,
            connection_field="edge_followed_by",
            end_cursor=end_cursor,
            count=count,
            query_hash=query_hash,
        )

    async def user_followers_web_gql_page_result(
        self,
        user_id: str,
        *,
        end_cursor: str = "",
        count: int = DEFAULT_PUBLIC_GRAPHQL_FOLLOWERS_COUNT,
        doc_id: str = PUBLIC_WEB_FOLLOWERS_DOC_ID,
    ) -> UserListPage:
        """Fetch one modern web Relay followers page by persisted doc id.

        The request mirrors Instagram's ``usePolarisGetFollowListQuery``
        operation and returns its opaque Relay cursor without consuming it.
        The registered ``doc_id`` is intentionally overridable because web
        operations rotate independently of this package. One call performs
        exactly one HTTP attempt; durable callers own retries and checkpoints.
        """
        return await self._user_web_gql_page_result(
            user_id,
            connection_field=PUBLIC_WEB_FOLLOWERS_CONNECTION,
            is_follower_list=True,
            end_cursor=end_cursor,
            count=count,
            doc_id=doc_id,
        )

    async def user_following_web_gql_page_result(
        self,
        user_id: str,
        *,
        end_cursor: str = "",
        count: int = DEFAULT_PUBLIC_GRAPHQL_FOLLOWING_COUNT,
        doc_id: str = PUBLIC_WEB_FOLLOWERS_DOC_ID,
    ) -> UserListPage:
        """Fetch one modern web Relay following page by persisted doc id."""
        return await self._user_web_gql_page_result(
            user_id,
            connection_field=PUBLIC_WEB_FOLLOWING_CONNECTION,
            is_follower_list=False,
            end_cursor=end_cursor,
            count=count,
            doc_id=doc_id,
        )

    async def _user_web_gql_page_result(
        self,
        user_id: str,
        *,
        connection_field: str,
        is_follower_list: bool,
        end_cursor: str,
        count: int,
        doc_id: str,
    ) -> UserListPage:
        if connection_field not in {PUBLIC_WEB_FOLLOWERS_CONNECTION, PUBLIC_WEB_FOLLOWING_CONNECTION}:
            raise ValueError("unsupported public web GraphQL follow-list connection")
        if not 1 <= count <= MAX_PUBLIC_GRAPHQL_USER_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_PUBLIC_GRAPHQL_USER_COUNT}")
        normalized_doc_id = str(doc_id or "").strip()
        if not re.fullmatch(r"[0-9]{8,40}", normalized_doc_id):
            raise ValueError("doc_id must be an 8-40 digit value")

        normalized_cursor = str(end_cursor or "").strip()
        variables: Dict[str, Any] = {
            "after": normalized_cursor or None,
            "before": None,
            "count": count,
            "first": count,
            "isFollowerList": is_follower_list,
            "last": None,
            "query": "",
            "userID": str(user_id),
        }
        data = await self.public_doc_id_graphql_request(
            normalized_doc_id,
            variables,
            retries_count=1,
            friendly_name=PUBLIC_WEB_FOLLOWERS_FRIENDLY_NAME,
            web_headers=True,
            headers={"X-Root-Field-Name": PUBLIC_WEB_FOLLOWERS_CONNECTION},
        )
        connection = data.get(connection_field) if isinstance(data, dict) else None
        if not isinstance(connection, dict):
            raise ClientGraphqlError("Missing public web GraphQL followers payload")
        return self._coerce_public_follow_list_page(data, connection)

    async def _user_public_gql_page_result(
        self,
        user_id: str,
        *,
        connection_field: str,
        end_cursor: str,
        count: int,
        query_hash: str,
    ) -> UserListPage:
        if connection_field not in {"edge_followed_by", "edge_follow"}:
            raise ValueError("unsupported public GraphQL follow-list connection")
        if not 1 <= count <= MAX_PUBLIC_GRAPHQL_USER_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_PUBLIC_GRAPHQL_USER_COUNT}")
        normalized_query_hash = str(query_hash or "").strip()
        if not re.fullmatch(r"[a-f0-9]{32}", normalized_query_hash):
            raise ValueError("query_hash must be a lowercase 32-character hex value")

        user_id = str(user_id)
        variables: Dict[str, Any] = {
            "id": user_id,
            "include_reel": True,
            "fetch_mutual": False,
            "first": count,
        }
        normalized_cursor = str(end_cursor or "").strip()
        if normalized_cursor:
            variables["after"] = normalized_cursor

        self.inject_sessionid_to_public()
        data = await self.public_graphql_request(
            variables,
            query_hash=normalized_query_hash,
            retries_count=1,
        )
        user = data.get("user") if isinstance(data, dict) else None
        if user is None:
            raise UserNotFound(user_id=user_id, **(data if isinstance(data, dict) else {}))
        if not isinstance(user, dict):
            raise ClientGraphqlError("Invalid public GraphQL user payload")
        connection = user.get(connection_field)
        if not isinstance(connection, dict):
            raise ClientGraphqlError("Missing public GraphQL follow-list payload")

        return self._coerce_public_follow_list_page(data, connection)

    def _coerce_public_follow_list_page(
        self,
        data: Dict[str, Any],
        connection: Dict[str, Any],
    ) -> UserListPage:
        edges = connection.get("edges")
        edges = edges if isinstance(edges, list) else []
        raw_users = [edge["node"] for edge in edges if isinstance(edge, dict) and isinstance(edge.get("node"), dict)]
        users = [extract_user_short(raw_user) for raw_user in raw_users]
        page_info = connection.get("page_info")
        page_info = page_info if isinstance(page_info, dict) else {}
        has_more = normalize_collection_has_more(
            {"page_info": page_info},
            next_cursor="",
        )
        next_cursor, cursor_field = normalize_collection_cursor(
            {"page_info": page_info},
        )
        if has_more is not True:
            next_cursor = ""
            cursor_field = ""
        http_status, response_bytes = response_observability(self.last_public_response)
        return UserListPage(
            users=users,
            next_cursor=next_cursor,
            cursor_field=cursor_field,
            has_more=has_more,
            route="public_graphql",
            response_keys=safe_mapping_keys(data),
            root_keys=safe_mapping_keys(connection),
            raw_user_count=len(raw_users),
            http_status=http_status,
            response_bytes=response_bytes,
        )

    async def user_followers_gql(self, user_id: str, amount: int = 0) -> List[UserShort]:
        """
        Get user's followers information by Public Graphql API

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of media to return, default is 0 - Inf

        Returns
        -------
        List[UserShort]
            List of objects of User type
        """
        users, _ = await self.user_followers_gql_chunk(str(user_id), amount)
        if amount:
            users = users[:amount]
        return users

    async def user_followers_v1_chunk(
        self,
        user_id: str,
        max_amount: int = 0,
        max_id: str = "",
        order: Optional[FOLLOWERS_ORDER] = None,
    ) -> Tuple[List[UserShort], str]:
        """
        Get user's followers information by Private Mobile API and max_id (cursor)

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        max_amount: int, optional
            Maximum number of users to return, default is 0 - Inf
        max_id: str, optional
            Max ID, default value is empty String
        order: str, optional
            Followers sort order: date_followed_latest or date_followed_earliest

        Returns
        -------
        Tuple[List[UserShort], str]
            Tuple of List of users and max_id
        """
        unique_set = set()
        seen_cursors = {str(max_id or "")}
        users: List[UserShort] = []
        while True:
            page_amount = MAX_USER_COUNT
            if max_amount:
                page_amount = min(max_amount - len(users), MAX_USER_COUNT)
            page, max_id = await self.user_followers_v1_page(
                user_id,
                max_id=max_id,
                count=page_amount,
                order=order,
            )
            for user in page:
                if user.pk in unique_set:
                    continue
                unique_set.add(user.pk)
                users.append(user)
            if not max_id or (max_amount and len(users) >= max_amount):
                break
            normalized_cursor = str(max_id)
            if normalized_cursor in seen_cursors:
                raise ClientError("Private followers cursor repeated")
            seen_cursors.add(normalized_cursor)
        return users, max_id

    async def user_followers_v1_page(
        self,
        user_id: str,
        *,
        max_id: str = "",
        count: int = MAX_USER_COUNT,
        order: Optional[FOLLOWERS_ORDER] = None,
    ) -> Tuple[List[UserShort], str]:
        """Fetch exactly one Private Mobile API page of followers.

        The next cursor is returned without being consumed so callers can
        durably persist both the users and cursor as one checkpoint.
        """
        page = await self.user_followers_v1_page_result(
            user_id,
            max_id=max_id,
            count=count,
            order=order,
        )
        return page.users, page.next_cursor

    async def user_followers_v1_page_result(
        self,
        user_id: str,
        *,
        max_id: str = "",
        count: int = MAX_USER_COUNT,
        order: Optional[FOLLOWERS_ORDER] = None,
    ) -> UserListPage:
        """Fetch one followers page with lossless pagination metadata."""
        if not 1 <= count <= MAX_USER_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_USER_COUNT}")
        params = {
            "count": count,
            "rank_token": self.rank_token,
            "search_surface": "follow_list_page",
            "query": "",
            "enable_groups": "true",
        }
        if order:
            params["order"] = order
        if max_id:
            params["max_id"] = max_id
        result = await self.private_request(
            f"friendships/{user_id}/followers/",
            params=params,
            retry_transient=False,
            retry_without_cursor=False,
        )
        raw_users = result.get("users") if isinstance(result.get("users"), list) else []
        users = [extract_user_short(user) for user in raw_users]
        next_cursor, cursor_field = normalize_collection_cursor(result)
        http_status, response_bytes = response_observability(self.last_response)
        return UserListPage(
            users=users,
            next_cursor=next_cursor,
            cursor_field=cursor_field,
            has_more=normalize_collection_has_more(result, next_cursor=next_cursor),
            route="private_v1",
            response_keys=safe_mapping_keys(result),
            root_keys=safe_mapping_keys(result),
            raw_user_count=len(raw_users),
            http_status=http_status,
            response_bytes=response_bytes,
            page_size=normalize_collection_page_size(result),
            big_list=normalize_collection_bool(result, "big_list"),
            should_limit_list_of_followers=normalize_collection_bool(
                result,
                "should_limit_list_of_followers",
            ),
        )

    async def user_followers_v1(
        self,
        user_id: str,
        amount: int = 0,
        order: Optional[FOLLOWERS_ORDER] = None,
    ) -> List[UserShort]:
        """
        Get user's followers information by Private Mobile API

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of media to return, default is 0 - Inf
        order: str, optional
            Followers sort order: date_followed_latest or date_followed_earliest

        Returns
        -------
        List[UserShort]
            List of objects of User type
        """
        users, _ = await self.user_followers_v1_chunk(str(user_id), amount, order=order)
        if amount:
            users = users[:amount]
        return users

    def iter_user_followers_v1(
        self,
        user_id: str,
        amount: int = 0,
        page_size: int = MAX_USER_COUNT,
        order: Optional[FOLLOWERS_ORDER] = None,
    ) -> AsyncIterator[UserShort]:
        """
        Iterate over user's followers by Private Mobile API.

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of users to yield, default is 0 - Inf
        page_size: int, optional
            Maximum number of users to fetch per page, default is 200
        order: str, optional
            Followers sort order: date_followed_latest or date_followed_earliest

        Returns
        -------
        AsyncIterator[UserShort]
            Async iterator of UserShort objects
        """
        user_id = str(user_id)

        async def fetch_page(max_id: Optional[str], max_amount: int) -> Tuple[List[UserShort], Optional[str]]:
            if order:
                return await self.user_followers_v1_chunk(
                    user_id,
                    max_amount=max_amount,
                    max_id=max_id or "",
                    order=order,
                )
            return await self.user_followers_v1_chunk(user_id, max_amount=max_amount, max_id=max_id or "")

        return iter_paginated(fetch_page, amount=amount, page_size=page_size, initial_cursor="")

    @staticmethod
    def _private_graphql_root(data: Dict, root_field_name: str) -> Dict:
        payload = data.get("data") or data
        if not isinstance(payload, dict):
            return {}
        root = payload.get(root_field_name)
        if isinstance(root, dict):
            return root
        for key, value in payload.items():
            if root_field_name in str(key) and isinstance(value, dict):
                return value
        return {}

    async def user_followers_private_gql_chunk(
        self,
        user_id: str,
        max_amount: int = 0,
        max_id: Optional[Union[str, int]] = None,
        rank_token: Optional[str] = None,
        order: Optional[FOLLOWERS_ORDER] = None,
        priority: str = "u=3, i",
        client_doc_id: Optional[str] = None,
        query_profile: FOLLOW_LIST_QUERY_PROFILE = "canonical",
    ) -> Tuple[List[UserShort], Optional[str]]:
        """
        Get user's followers information by Private GraphQL API and max_id.

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        max_amount: int, optional
            Maximum number of users to return from the fetched chunk, default is 0 - full chunk
        max_id: str, optional
            The cursor from which it is worth continuing to receive the list of followers
        rank_token: str, optional
            Rank token for the follow list request. Defaults to client rank_token
        order: FOLLOWERS_ORDER, optional
            Followers sort order: date_followed_latest or date_followed_earliest
        priority: str, optional
            GraphQL request priority header captured from the Android app

        Returns
        -------
        Tuple[List[UserShort], str]
            List of users and next max_id cursor
        """
        page = await self.user_followers_private_gql_page_result(
            user_id,
            max_id=max_id,
            rank_token=rank_token,
            order=order,
            priority=priority,
            client_doc_id=client_doc_id,
            query_profile=query_profile,
        )
        users = page.users[:max_amount] if max_amount else page.users
        return users, page.next_cursor or None

    async def user_followers_private_gql_page_result(
        self,
        user_id: str,
        *,
        max_id: Optional[Union[str, int]] = None,
        rank_token: Optional[str] = None,
        order: Optional[FOLLOWERS_ORDER] = None,
        priority: str = "u=3, i",
        client_doc_id: Optional[str] = None,
        query_profile: FOLLOW_LIST_QUERY_PROFILE = "canonical",
    ) -> UserListPage:
        """Fetch one private GraphQL followers page with safe shape metadata."""
        user_id = str(user_id)
        request_kwargs: Dict[str, Any] = {
            "max_id": max_id,
            "order": order,
            "priority": priority,
        }
        if client_doc_id is not None:
            request_kwargs["client_doc_id"] = client_doc_id
        if query_profile != "canonical":
            request_kwargs["query_profile"] = query_profile
        result = await self.private_graphql_followers_list(
            user_id,
            rank_token or self.rank_token,
            **request_kwargs,
        )
        followers = self._private_graphql_root(result, "xdt_api__v1__friendships__followers")
        if not followers:
            raise ClientGraphqlError("Missing private GraphQL followers payload")
        raw_users = self._private_graphql_users(followers)
        users = [extract_user_short(user) for user in raw_users]
        next_cursor, cursor_field = normalize_collection_cursor(followers)
        http_status, response_bytes = response_observability(self.last_response)
        return UserListPage(
            users=users,
            next_cursor=next_cursor,
            cursor_field=cursor_field,
            has_more=normalize_collection_has_more(followers, next_cursor=next_cursor),
            route="private_graphql",
            response_keys=safe_mapping_keys(result.get("data") or result),
            root_keys=safe_mapping_keys(followers),
            raw_user_count=len(raw_users),
            http_status=http_status,
            response_bytes=response_bytes,
            page_size=normalize_collection_page_size(followers),
            big_list=normalize_collection_bool(followers, "big_list"),
            should_limit_list_of_followers=normalize_collection_bool(
                followers,
                "should_limit_list_of_followers",
            ),
        )

    async def user_following_private_gql_page_result(
        self,
        user_id: str,
        *,
        max_id: Optional[Union[str, int]] = None,
        rank_token: Optional[str] = None,
        order: Optional[FOLLOWERS_ORDER] = None,
        priority: str = "u=3, i",
        client_doc_id: Optional[str] = None,
        query_profile: FOLLOW_LIST_QUERY_PROFILE = "canonical",
    ) -> UserListPage:
        """Fetch one private GraphQL following page with safe shape metadata."""
        user_id = str(user_id)
        request_kwargs: Dict[str, Any] = {
            "max_id": max_id,
            "order": order,
            "priority": priority,
        }
        if client_doc_id is not None:
            request_kwargs["client_doc_id"] = client_doc_id
        if query_profile != "canonical":
            request_kwargs["query_profile"] = query_profile
        result = await self.private_graphql_following_list(
            user_id,
            rank_token or self.rank_token,
            **request_kwargs,
        )
        following = self._private_graphql_root(result, "xdt_api__v1__friendships__following")
        if not following:
            raise ClientGraphqlError("Missing private GraphQL following payload")
        raw_users = self._private_graphql_users(following)
        users = [extract_user_short(user) for user in raw_users]
        next_cursor, cursor_field = normalize_collection_cursor(following)
        http_status, response_bytes = response_observability(self.last_response)
        return UserListPage(
            users=users,
            next_cursor=next_cursor,
            cursor_field=cursor_field,
            has_more=normalize_collection_has_more(following, next_cursor=next_cursor),
            route="private_graphql",
            response_keys=safe_mapping_keys(result.get("data") or result),
            root_keys=safe_mapping_keys(following),
            raw_user_count=len(raw_users),
            http_status=http_status,
            response_bytes=response_bytes,
            page_size=normalize_collection_page_size(following),
            big_list=normalize_collection_bool(following, "big_list"),
            should_limit_list_of_followers=normalize_collection_bool(
                following,
                "should_limit_list_of_followers",
            ),
        )

    @staticmethod
    def _private_graphql_users(root: Dict) -> List[Dict]:
        users = root.get("users")
        if isinstance(users, list):
            return [user for user in users if isinstance(user, dict)]
        edges = root.get("edges")
        if not isinstance(edges, list):
            return []
        nodes: List[Dict] = []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                nodes.append(node)
        return nodes

    async def user_followers_private_gql(
        self,
        user_id: str,
        amount: int = 0,
        rank_token: Optional[str] = None,
        order: Optional[FOLLOWERS_ORDER] = None,
        priority: str = "u=3, i",
        client_doc_id: Optional[str] = None,
        query_profile: FOLLOW_LIST_QUERY_PROFILE = "canonical",
    ) -> List[UserShort]:
        """
        Get user's followers information by Private GraphQL API.

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of users to return, default is 0 - Inf
        rank_token: str, optional
            Rank token for the follow list request. Defaults to client rank_token
        order: FOLLOWERS_ORDER, optional
            Followers sort order: date_followed_latest or date_followed_earliest
        priority: str, optional
            GraphQL request priority header captured from the Android app

        Returns
        -------
        List[UserShort]
            List of objects of UserShort type
        """
        users: List[UserShort] = []
        unique_ids: set[str] = set()
        seen_cursors = {""}
        max_id: Optional[Union[str, int]] = None
        while True:
            chunk_amount = max(amount - len(users), 0) if amount else 0
            chunk, max_id = await self.user_followers_private_gql_chunk(
                user_id,
                max_amount=chunk_amount,
                max_id=max_id,
                rank_token=rank_token,
                order=order,
                priority=priority,
                client_doc_id=client_doc_id,
                query_profile=query_profile,
            )
            for user in chunk:
                normalized_user_id = str(user.pk)
                if normalized_user_id in unique_ids:
                    continue
                unique_ids.add(normalized_user_id)
                users.append(user)
            if amount and len(users) >= amount:
                break
            if not max_id or not chunk:
                break
            normalized_cursor = str(max_id)
            if normalized_cursor in seen_cursors:
                raise ClientGraphqlError("Private GraphQL followers cursor repeated")
            seen_cursors.add(normalized_cursor)
        if amount:
            users = users[:amount]
        return users

    async def user_followers(
        self,
        user_id: str,
        amount: int = 0,
        order: Optional[FOLLOWERS_ORDER] = None,
        use_cache: bool = True,
    ) -> Dict[str, UserShort]:
        """
        Get user's followers

        Parameters
        ----------
        user_id: str
            User id of an instagram account
        amount: int, optional
            Maximum number of media to return, default is 0 - Inf
        order: FOLLOWERS_ORDER, optional
            Followers sort order: date_followed_latest or date_followed_earliest.
            Sorted requests use the private mobile endpoint.
        use_cache: bool, optional
            Whether or not to use information from cache, default value is True

        Returns
        -------
        Dict[str, UserShort]
            Dict of user_id and User object
        """
        user_id = str(user_id)
        if order:
            users = await self.user_followers_v1(user_id, amount, order=order)
            return {user.pk: user for user in users}
        users = self._users_followers.get(user_id, {})
        if not use_cache or not users or (amount and len(users) < amount):
            if self._has_private_auth():
                try:
                    users = await self.user_followers_v1(user_id, amount)
                    if self.last_json.get("should_limit_list_of_followers") and (not amount or len(users) < amount):
                        users = await self.user_followers_gql(user_id, amount)
                except Exception as e:
                    if not isinstance(e, ClientError):
                        self.logger.exception(e)
                    users = await self.user_followers_gql(user_id, amount)
            else:
                try:
                    users = await self.user_followers_gql(user_id, amount)
                except Exception as e:
                    if not isinstance(e, ClientError):
                        self.logger.exception(e)
                    users = await self.user_followers_v1(user_id, amount)
            self._users_followers[user_id] = {user.pk: user for user in users}
        followers = self._users_followers[user_id]
        if amount and len(followers) > amount:
            followers = dict(list(followers.items())[:amount])
        return followers

    async def user_follow_requests_chunk(self, max_amount: int = 0, max_id: str = "") -> Tuple[List[UserShort], str]:
        """
        Get pending incoming follow requests by Private Mobile API

        Parameters
        ----------
        max_amount: int, optional
            Maximum number of follow requests to return, default is 0 - Inf
        max_id: str, optional
            Cursor for the next chunk

        Returns
        -------
        Tuple[List[UserShort], str]
            List of UserShort objects and max_id cursor
        """
        if not self.user_id:
            raise PreLoginRequired
        users = []
        unique_set = set()
        while True:
            params = {"count": max_amount or MAX_USER_COUNT}
            if max_id:
                params["max_id"] = max_id
            result = await self.private_request("friendships/pending/", params=params)
            for user in result.get("users", []):
                user = extract_user_short(user)
                if user.pk in unique_set:
                    continue
                unique_set.add(user.pk)
                users.append(user)
            max_id = result.get("next_max_id")
            if not max_id or (max_amount and len(users) >= max_amount):
                break
        return users, max_id

    async def user_follow_requests(self, amount: int = 0) -> List[UserShort]:
        """
        Get pending incoming follow requests by Private Mobile API

        Parameters
        ----------
        amount: int, optional
            Maximum number of follow requests to return, default is 0 - Inf

        Returns
        -------
        List[UserShort]
            List of UserShort objects
        """
        users, _ = await self.user_follow_requests_chunk(amount)
        if amount:
            users = users[:amount]
        return users

    async def user_follow_request_approve(self, user_id: str) -> bool:
        """
        Approve a pending incoming follow request

        Parameters
        ----------
        user_id: str

        Returns
        -------
        bool
            A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        data = self.with_action_data({"user_id": user_id})
        result = await self.private_request(f"friendships/approve/{user_id}/", data)
        friendship_status = result.get("friendship_status", {})
        if "followed_by" in friendship_status:
            return friendship_status["followed_by"] is True
        return result.get("status") == "ok"

    async def user_follow_request_decline(self, user_id: str) -> bool:
        """
        Decline a pending incoming follow request

        Parameters
        ----------
        user_id: str

        Returns
        -------
        bool
            A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        data = self.with_action_data({"user_id": user_id})
        result = await self.private_request(f"friendships/ignore/{user_id}/", data)
        friendship_status = result.get("friendship_status", {})
        if "followed_by" in friendship_status:
            return friendship_status["followed_by"] is False
        return result.get("status") == "ok"

    async def user_follow_requests_approve(self, user_ids: List[str]) -> Dict[str, bool]:
        """
        Approve pending incoming follow requests

        Parameters
        ----------
        user_ids: List[str]

        Returns
        -------
        Dict[str, bool]
            Dict of user_id and result
        """
        return {str(user_id): await self.user_follow_request_approve(str(user_id)) for user_id in user_ids}

    async def user_follow_requests_decline(self, user_ids: List[str]) -> Dict[str, bool]:
        """
        Decline pending incoming follow requests

        Parameters
        ----------
        user_ids: List[str]

        Returns
        -------
        Dict[str, bool]
            Dict of user_id and result
        """
        return {str(user_id): await self.user_follow_request_decline(str(user_id)) for user_id in user_ids}

    async def user_follow(self, user_id: str) -> bool:
        """
        Follow a user

        Parameters
        ----------
        user_id: str

        Returns
        -------
        bool
            A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        current_user_id = str(self.user_id)
        following_cache = self._users_following.get(current_user_id)
        if user_id in (following_cache or {}):
            self.logger.debug("User %s already followed", user_id)
            return False
        try:
            relationship = await self.user_friendship_v1(user_id)
        except Exception as e:
            logger.debug("Unable to pre-check friendship for %s before follow: %r", user_id, e)
            relationship = None
        if relationship and (relationship.following or relationship.outgoing_request):
            self.logger.debug("User %s already followed or requested", user_id)
            return False
        data = self.with_action_data(
            {
                "user_id": user_id,
                "_uid": str(self.user_id),
                "include_follow_friction_check": "1",
                "container_module": "profile",
            }
        )
        result = await self.private_request(f"friendships/create/{user_id}/", data)
        friendship_status = result["friendship_status"]
        followed = friendship_status.get("following") is True or friendship_status.get("outgoing_request") is True
        if followed and following_cache is not None:
            following_cache[user_id] = self._userhorts_cache.get(user_id) or UserShort(pk=user_id)
        return followed

    async def user_unfollow(self, user_id: str) -> bool:
        """
        Unfollow a user

        Parameters
        ----------
        user_id: str

        Returns
        -------
        bool
            A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        data = self.with_action_data(
            {
                "user_id": user_id,
                "_uid": str(self.user_id),
                "container_module": "profile",
            }
        )
        result = await self.private_request(f"friendships/destroy/{user_id}/", data)
        if self.user_id in self._users_following:
            self._users_following[self.user_id].pop(user_id, None)
        return result["friendship_status"]["following"] is False

    async def user_block(self, user_id: str, surface: UserBlockSurface = "profile") -> bool:
        """
        Block a User

        Parameters
        ----------
        user_id: str
            User ID of an Instagram account
        surface: UserBlockSurface, optional
            Surface used by Instagram for the block action, default "profile"; use
            "direct_thread_info" from Direct thread info.

        Returns
        -------
        bool
            A boolean value
        """
        data = {
            "surface": surface,
            "is_auto_block_enabled": "false",
            "user_id": user_id,
            "_uid": self.user_id,
            "_uuid": self.uuid,
        }
        if surface == "direct_thread_info":
            data["client_request_id"] = self.request_id

        result = await self.private_request(f"friendships/block/{user_id}/", data)
        assert result.get("status", "") == "ok"

        return result.get("friendship_status", {}).get("blocking") is True

    async def user_unblock(self, user_id: str, surface: UserBlockSurface = "profile") -> bool:
        """
        Unlock a User

        Parameters
        ----------
        user_id: str
            User ID of an Instagram account
        surface: UserBlockSurface, optional
            Surface used by Instagram for the unblock action, default "profile"; use
            "direct_thread_info" from Direct thread info.

        Returns
        -------
        bool
            A boolean value
        """
        data = {
            "container_module": surface,
            "user_id": user_id,
            "_uid": self.user_id,
            "_uuid": self.uuid,
        }
        if surface == "direct_thread_info":
            data["client_request_id"] = self.request_id

        result = await self.private_request(f"friendships/unblock/{user_id}/", data)
        assert result.get("status", "") == "ok"

        return result.get("friendship_status", {}).get("blocking") is False

    async def user_report(self, user_id: str, reason: USER_REPORT_REASON = "spam") -> bool:
        """
        Report a User

        Parameters
        ----------
        user_id: str
            User ID of an Instagram account
        reason: str, optional
            Report reason (default "spam")

        Returns
        -------
        bool
            True if Instagram returns the report confirmation state
        """
        user_id = str(user_id)
        if reason not in USER_REPORT_REASONS:
            raise ValueError(
                f'Unsupported user report reason "{reason}". Supported reasons: {tuple(USER_REPORT_REASONS)}'
            )

        result = await self.private_request(
            "reports/get_frx_prompt/",
            data={
                "_uuid": self.uuid,
                "container_module": "profile",
                "entry_point": "1",
                "frx_prompt_request_type": "1",
                "is_dark_mode": "false",
                "location": "2",
                "nua_action": "",
                "object_id": user_id,
                "object_type": "5",
            },
            with_signature=False,
        )
        response = result.get("response", result)
        context = response.get("context")
        if not context:
            raise ClientError("Instagram report flow did not return an initial context", **result)

        for tag in USER_REPORT_REASONS[reason]:
            result = await self.private_request(
                "reports/get_frx_prompt/",
                data={
                    "_uuid": self.uuid,
                    "context": context,
                    "frx_prompt_request_type": "2",
                    "is_dark_mode": "false",
                    "nua_action": "",
                    "selected_tag_types": dumps([tag]),
                },
                with_signature=False,
            )
            response = result.get("response", result)
            context = response.get("context")
            if not context:
                raise ClientError(f'Instagram report flow did not return context after tag "{tag}"', **result)

        return bool(response.get("follow_up_actions"))

    async def user_remove_follower(self, user_id: str) -> bool:
        """
        Remove a follower

        Parameters
        ----------
        user_id: str

        Returns
        -------
        bool
            A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        data = self.with_action_data({"user_id": str(user_id)})
        result = await self.private_request(f"friendships/remove_follower/{user_id}/", data)
        if self.user_id in self._users_followers:
            self._users_followers[self.user_id].pop(user_id, None)
        return result["friendship_status"]["followed_by"] is False

    async def mute_posts_from_follow(self, user_id: str, revert: bool = False) -> bool:
        """
        Mute posts from following user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        revert: bool, optional
            Unmute when True

        Returns
        -------
        bool
            A boolean value
        """
        user_id = str(user_id)
        name = "unmute" if revert else "mute"
        result = await self.private_request(
            f"friendships/{name}_posts_or_story_from_follow/",
            {
                # "media_id": media_pk,  # when feed_timeline
                "target_posts_author_id": str(user_id),
                "container_module": "media_mute_sheet",  # or "feed_timeline"
            },
        )
        return result["status"] == "ok"

    async def unmute_posts_from_follow(self, user_id: str) -> bool:
        """
        Unmute posts from following user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User

        Returns
        -------
        bool
            A boolean value
        """
        return await self.mute_posts_from_follow(user_id, True)

    async def mute_stories_from_follow(self, user_id: str, revert: bool = False) -> bool:
        """
        Mute stories from following user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        revert: bool, optional
            Unmute when True

        Returns
        -------
        bool
            A boolean value
        """
        user_id = str(user_id)
        name = "unmute" if revert else "mute"
        result = await self.private_request(
            f"friendships/{name}_posts_or_story_from_follow/",
            {
                # "media_id": media_pk,  # when feed_timeline
                "target_reel_author_id": str(user_id),
                "container_module": "media_mute_sheet",  # or "feed_timeline"
            },
        )
        return result["status"] == "ok"

    async def unmute_stories_from_follow(self, user_id: str) -> bool:
        """
        Unmute stories from following user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User

        Returns
        -------
        bool
            A boolean value
        """
        return await self.mute_stories_from_follow(user_id, True)

    async def enable_posts_notifications(self, user_id: str, disable: bool = False) -> bool:
        """
        Enable post notifications of a user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        disable: bool, optional
            Unfavorite when True

        Returns
        -------
        bool
            A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        data = self.with_action_data({"user_id": user_id, "_uid": self.user_id})
        name = "unfavorite" if disable else "favorite"
        result = await self.private_request(f"friendships/{name}/{user_id}/", data)
        return result["status"] == "ok"

    async def disable_posts_notifications(self, user_id: str) -> bool:
        """
        Disable post notifications of a user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        Returns
        -------
        bool
            A boolean value
        """
        return await self.enable_posts_notifications(user_id, True)

    async def enable_videos_notifications(self, user_id: str, revert: bool = False) -> bool:
        """
        Enable videos notifications of a user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        revert: bool, optional
            Unfavorite when True

        Returns
        -------
        bool
        A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        data = self.with_action_data({"user_id": user_id, "_uid": self.user_id})
        name = "unfavorite" if revert else "favorite"
        result = await self.private_request(f"friendships/{name}_for_igtv/{user_id}/", data)
        return result["status"] == "ok"

    async def disable_videos_notifications(self, user_id: str) -> bool:
        """
        Disable videos notifications of a user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        Returns
        -------
        bool
            A boolean value
        """
        return await self.enable_videos_notifications(user_id, True)

    async def enable_reels_notifications(self, user_id: str, revert: bool = False) -> bool:
        """
        Enable reels notifications of a user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        revert: bool, optional
            Unfavorite when True

        Returns
        -------
        bool
        A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        data = self.with_action_data({"user_id": user_id, "_uid": self.user_id})
        name = "unfavorite" if revert else "favorite"
        result = await self.private_request(f"friendships/{name}_for_clips/{user_id}/", data)
        return result["status"] == "ok"

    async def disable_reels_notifications(self, user_id: str) -> bool:
        """
        Disable reels notifications of a user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        Returns
        -------
        bool
            A boolean value
        """
        return await self.enable_reels_notifications(user_id, True)

    async def enable_stories_notifications(self, user_id: str, revert: bool = False) -> bool:
        """
        Enable stories notifications of a user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        revert: bool, optional
            Unfavorite when True

        Returns
        -------
        bool
        A boolean value
        """
        if not self.user_id:
            raise PreLoginRequired
        user_id = str(user_id)
        data = self.with_action_data({"user_id": user_id, "_uid": self.user_id})
        name = "unfavorite" if revert else "favorite"
        result = await self.private_request(f"friendships/{name}_for_stories/{user_id}/", data)
        return result["status"] == "ok"

    async def disable_stories_notifications(self, user_id: str) -> bool:
        """
        Disable stories notifications of a user

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        Returns
        -------
        bool
            A boolean value
        """
        return await self.enable_stories_notifications(user_id, True)

    async def close_friend_add(self, user_id: str):
        """
        Add to Close Friends List

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        Returns
        -------
        bool
            A boolean value
        """
        assert self.user_id, "Login required"
        user_id = str(user_id)
        data = {
            "block_on_empty_thread_creation": "false",
            "module": "CLOSE_FRIENDS_V2_SEARCH",
            "source": "audience_manager",
            "_uid": self.user_id,
            "_uuid": self.uuid,
            "remove": [],
            "add": [user_id],
        }
        result = await self.private_request("friendships/set_besties/", data)
        return json_value(result, "friendship_statuses", user_id, "is_bestie")

    async def close_friend_remove(self, user_id: str):
        """
        Remove from Close Friends List

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        Returns
        -------
        bool
            A boolean value
        """
        assert self.user_id, "Login required"
        user_id = str(user_id)
        data = {
            "block_on_empty_thread_creation": "false",
            "module": "CLOSE_FRIENDS_V2_SEARCH",
            "source": "audience_manager",
            "_uid": self.user_id,
            "_uuid": self.uuid,
            "remove": [user_id],
            "add": [],
        }
        result = await self.private_request("friendships/set_besties/", data)
        return json_value(result, "friendship_statuses", user_id, "is_bestie") is False

    async def creator_info(self, user_id: str, entry_point: str = "direct_thread") -> Tuple[UserShort, Dict]:
        """
        Retrieves Creator's information

        Parameters
        ----------
        user_id: str
            Unique identifier of a User
        entry_point: str, optional
            Entry point for retrieving, default - direct_thread
            When passing self_profile, own user_id must be provided

        Returns
        -------
        Tuple[UserShort, Dict]
            Retrieved User and his Creator's Info
        """
        assert self.user_id, "Login required"
        params = {
            "entry_point": entry_point,
            "surface_type": "android",
            "user_id": user_id,
        }

        result = await self.private_request("creator/creator_info/", params=params)
        assert result.get("status", "") == "ok"

        creator_info = result.get("user", {}).pop("creator_info", {})
        user = extract_user_short(result.get("user", {}))
        return (user, creator_info)

    async def user_guides_v1(self, user_id: int) -> List[Guide]:
        """
        Get guides by user_id

        Parameters
        ----------
        user_id: int

        Returns
        -------
        List[Guide]
            List of objects of Guide
        """
        user_id = int(user_id)
        result = await self.private_request(f"guides/user/{user_id}/")
        return [extract_guide_v1(item) for item in (result.get("guides") or [])]

    async def user_stream_by_username_v1(self, username: str) -> dict:
        """
        Get stream object from user name

        Parameters
        ----------
        username: str
            User name of an instagram account

        Returns
        -------
        Dict
            An object of user stream (user info)
        """
        username = self._normalize_username(username)
        data = {
            "is_prefetch": False,
            "entry_point": "profile",
            "from_module": "feed_timeline",
        }
        try:
            result = await self.private_request(f"users/{username}/usernameinfo_stream/", data=data)
        except ClientNotFoundError as e:
            raise UserNotFound(e, username=username, **self.last_json)
        except ClientError as e:
            raise UserNotFound(e, username=username, **self.last_json)
        return result

    async def user_stream_by_id_v1(self, user_id: str) -> dict:
        """
        Get user info-stream by pk (mirror of
        :meth:`user_stream_by_username_v1`).

        ``POST /users/{user_id}/info_stream/`` — IG's app-side surface
        for a profile fetch initiated from within the feed timeline
        flow. Returns the same streamed envelope as the username
        variant.

        Parameters
        ----------
        user_id: str
            Target user pk.

        Returns
        -------
        dict
            Parsed JSON response (typically with ``stream_rows``).
        """
        data = {
            "is_prefetch": False,
            "entry_point": "profile",
            "from_module": "feed_timeline",
        }
        try:
            result = await self.private_request(f"users/{user_id}/info_stream/", data=data)
        except ClientJSONDecodeError:
            response_text = getattr(getattr(self, "last_response", None), "text", "") or ""
            for line in response_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except ValueError:
                    break
            logger.exception("Unable to parse streamed user response for user_id %r", user_id)
            raise UserNotFound("User not found")
        except (ClientNotFoundError, ClientError) as e:
            logger.exception(
                "Client error user_stream_by_id_v1, exception: %r, user_id %r",
                e,
                user_id,
            )
            raise UserNotFound("User not found")
        return result

    async def _user_stream_collector(self, resp, id=None, username=None):
        """Collapse a stream_rows envelope into a single flat user dict.

        Each row in ``stream_rows`` carries a partial ``user`` payload;
        this merges them in order so later rows override earlier ones.
        Falls back to one extra fetch if the first response was
        empty (defensive behaviour matching observed IG quirks).
        """
        data = {}
        if isinstance(resp.get("user"), dict):
            data.update(resp["user"])
        for urow in resp.get("stream_rows", []):
            data.update(urow.get("user", {}))
        if data:
            data["pk"] = data.get("pk", data.get("pk_id"))
            return data
        logger.error("user_stream_collector: empty stream_rows, falling back: %r", resp)
        if username:
            resp = await self.user_stream_by_username_v1(username)
        elif id:
            resp = await self.user_stream_by_id_v1(id)
        else:
            raise UserNotFound(code_error=1257)
        return resp or self.last_json

    async def user_stream_by_id_flat(self, user_id: str) -> dict:
        """
        Flatten the streamed profile envelope for a target user pk
        into a single user dict.

        Convenience wrapper: calls :meth:`user_stream_by_id_v1` and
        merges all ``stream_rows[*].user`` partial payloads in order.

        Parameters
        ----------
        user_id: str
            Target user pk.

        Returns
        -------
        dict
            Merged user dict (with ``pk`` resolved from ``pk`` or
            ``pk_id`` whichever IG provided).
        """
        resp = await self.user_stream_by_id_v1(user_id)
        return await self._user_stream_collector(resp, id=user_id)

    async def user_stream_by_username_flat(self, username: str) -> dict:
        """
        Flatten the streamed profile envelope for a target username
        into a single user dict.

        Convenience wrapper: calls :meth:`user_stream_by_username_v1`
        and merges all ``stream_rows[*].user`` partial payloads in
        order.

        Parameters
        ----------
        username: str
            Target IG username.

        Returns
        -------
        dict
            Merged user dict.
        """
        resp = await self.user_stream_by_username_v1(username)
        return await self._user_stream_collector(resp, username=username)

    async def user_web_profile_info_v1(self, username: str) -> dict:
        """
        Web-scraper-style profile fetch via the private API.

        ``GET /users/web_profile_info/?username={username}`` — the
        same payload shape as the public ``api/v1/users/web_profile_info/``
        endpoint, but routed through the private host so it can carry
        a logged-in session and bypass some of the public-side rate
        limiting. Returns the inner ``data`` block (already unwrapped).

        Parameters
        ----------
        username: str
            Target IG username.

        Returns
        -------
        dict
            The user payload (from ``response['data']``).

        Raises
        ------
        UserNotFound
            ``data`` is missing from the response or the request 404'd.
        """
        username = self._normalize_username(username)
        try:
            result = await self.private_request(
                "users/web_profile_info/",
                params={"username": username},
            )
        except (ClientNotFoundError, ClientError) as e:
            raise UserNotFound(e, username=username, **self.last_json)
        if data := result.get("data", {}):
            return data
        raise UserNotFound("Username not found", username=username, **self.last_json)

    async def feed_user_stream_item(
        self,
        item_id: str,
        is_pull_to_refresh: bool = False,
    ) -> dict:
        """
        Fetch the streamed feed for a user (profile grid stream).

        ``POST /feed/user_stream/{item_id}/`` — returns the per-user feed
        delivered as a streaming response. ``item_id`` is typically the
        target user pk. Sends the standard ``_uuid`` payload IG expects on
        POST endpoints.

        Parameters
        ----------
        item_id: str
            Target user pk (or other stream resource id).
        is_pull_to_refresh: bool, default False
            Set to True to mimic a pull-to-refresh fetch (sends
            ``is_pull_to_refresh="true"``).

        Returns
        -------
        dict
            Parsed JSON response. Streaming envelopes are aggregated by
            ``private_request`` into a ``stream_rows`` key when needed.
        """
        data = {
            "_uuid": self.uuid,
        }
        if is_pull_to_refresh:
            data["is_pull_to_refresh"] = "true"
        return await self.private_request(f"feed/user_stream/{item_id}/", data=data)

    async def private_graphql_followers_list(
        self,
        user_id: str,
        rank_token: str,
        client_doc_id: str = FOLLOWERS_LIST_CLIENT_DOC_ID,
        max_id: Optional[Union[str, int]] = None,
        priority: Optional[str] = None,
        order: Optional[FOLLOWERS_ORDER] = None,
        exclude_field_is_favorite: Optional[bool] = None,
        exclude_unused_fields: Optional[bool] = None,
        skip_preview_hashtags: bool = True,
        skip_hashtag_count: bool = True,
        query_profile: FOLLOW_LIST_QUERY_PROFILE = "canonical",
    ) -> dict:
        """
        Private-side ``FollowersList`` GraphQL query.

        Newer mobile-app surface that returns the followers list via
        ``i.instagram.com/graphql/query`` (root field
        ``xdt_api__v1__friendships__followers``). Prefer the higher-level
        ``user_followers_v1`` / ``user_followers_gql`` helpers when you
        just need a list of users — this is the raw doc-id wrapper.

        Parameters
        ----------
        user_id: str
            Target user pk.
        rank_token: str
            UUID-style rank token IG generates per follow-list session.
        client_doc_id: str, optional
            Numeric doc id of the registered query.
        max_id: int, optional
            Cursor for pagination.
        priority: str, optional
            ``Priority`` header value, e.g. ``"u=3, i"``.
        order: str, optional
            Follow-list ordering value such as ``"date_followed_latest"``.
        exclude_field_is_favorite, exclude_unused_fields: bool, optional
            Forwarded to the ``variables`` payload.

        Returns
        -------
        dict
            Raw GraphQL response.
        """
        variables = _followers_list_variables(
            user_id,
            rank_token,
            max_id=max_id,
            order=order,
            query_profile=query_profile,
        )
        if exclude_field_is_favorite is not None:
            variables["exclude_field_is_favorite"] = exclude_field_is_favorite
        if exclude_unused_fields is not None:
            variables["exclude_unused_fields"] = exclude_unused_fields
        return await self.private_graphql_query_request(
            friendly_name="FollowersList",
            root_field_name="xdt_api__v1__friendships__followers",
            variables=variables,
            client_doc_id=client_doc_id,
            priority=priority,
            extra_headers={"X-FB-RMD": "state=URL_ELIGIBLE"},
        )

    async def private_graphql_following_list(
        self,
        user_id: str,
        rank_token: str,
        client_doc_id: str = FOLLOWING_LIST_CLIENT_DOC_ID,
        max_id: Optional[Union[str, int]] = None,
        priority: Optional[str] = None,
        order: Optional[FOLLOWERS_ORDER] = None,
        exclude_field_is_favorite: Optional[bool] = None,
        exclude_unused_fields: Optional[bool] = None,
        skip_preview_hashtags: bool = True,
        skip_hashtag_count: bool = True,
        query_profile: FOLLOW_LIST_QUERY_PROFILE = "canonical",
    ) -> dict:
        """
        Private-side ``FollowingList`` GraphQL query.

        Mirror of ``private_graphql_followers_list`` for the following
        edge — root field ``xdt_api__v1__friendships__following``.
        """
        variables = _following_list_variables(
            user_id,
            rank_token,
            max_id=max_id,
            order=order,
            query_profile=query_profile,
            skip_preview_hashtags=skip_preview_hashtags,
            skip_hashtag_count=skip_hashtag_count,
        )
        if exclude_field_is_favorite is not None:
            variables["exclude_field_is_favorite"] = exclude_field_is_favorite
        if exclude_unused_fields is not None:
            variables["exclude_unused_fields"] = exclude_unused_fields
        return await self.private_graphql_query_request(
            friendly_name="FollowingList",
            root_field_name="xdt_api__v1__friendships__following",
            variables=variables,
            client_doc_id=client_doc_id,
            priority=priority,
            extra_headers={"X-FB-RMD": "state=URL_ELIGIBLE"},
        )

    async def private_graphql_clips_profile(
        self,
        target_user_id: str,
        client_doc_id: str = "209049231614685382737238866578",
        priority: str = None,
        initial_stream_count: int = 6,
        page_size: int = 12,
        no_of_medias_in_each_chunk: int = 6,
    ) -> dict:
        """
        Private-side ``ClipsProfileQuery`` GraphQL query.

        Returns the profile-grid Reels stream for ``target_user_id`` via
        ``i.instagram.com/graphql/query`` (root field
        ``xdt_user_clips_graphql``). For a parsed list of media use
        ``user_clips_v1`` instead — this is the raw doc-id wrapper.

        Parameters
        ----------
        target_user_id: str
            Target user pk.
        client_doc_id: str, optional
            Numeric doc id of the registered query.
        priority: str, optional
        initial_stream_count: int, default 6
        page_size: int, default 12
        no_of_medias_in_each_chunk: int, default 6

        Returns
        -------
        dict
            Raw GraphQL response (often a streamed envelope).
        """
        inner_data = {
            "target_user_id": str(target_user_id),
            # IG returns a multi-document NDJSON envelope when these are
            # True; turn them off so the response is a single JSON we can
            # parse with response.json(). Set them back to True if you
            # want raw streamed media chunks (you'll need to parse it
            # yourself from the raw .text).
            "should_stream_response": False,
            "sort_by_views": False,
            "max_id": None,
            "include_feed_video": True,
            "audience": None,
        }
        if page_size:
            inner_data["page_size"] = page_size
        if no_of_medias_in_each_chunk:
            inner_data["no_of_medias_in_each_chunk"] = no_of_medias_in_each_chunk
        variables = {
            "use_stream": False,
            "use_defer": False,
            "enable_video_versions_in_light_media": True,
            "exclude_caption_user_field": False,
            "enable_thumbnails_in_light_media": False,
            "enable_audience_in_light_media": False,
            "enable_clips_metadata_in_light_media": False,
            "exclude_main_user_field": False,
            "enable_likers_in_full_media": False,
            "data": inner_data,
            "stream_use_customized_batch": False,
        }
        if initial_stream_count:
            variables["initial_stream_count"] = initial_stream_count
        return await self.private_graphql_query_request(
            friendly_name="ClipsProfileQuery",
            root_field_name="xdt_user_clips_graphql",
            variables=variables,
            client_doc_id=client_doc_id,
            priority=priority,
        )

    async def private_graphql_inbox_tray_for_user(
        self,
        user_id: str,
        client_doc_id: str = "2035639076042015234490020607",
        priority: str = None,
    ) -> dict:
        """
        Private-side ``InboxTrayRequestForUserQuery`` GraphQL query.

        Returns the per-user direct-inbox tray digest (root field
        ``xdt_get_inbox_tray_items``).

        Parameters
        ----------
        user_id: str
            Target user pk.
        client_doc_id: str, optional
        priority: str, optional
        """
        variables = {
            "user_id": str(user_id),
            "should_fetch_content_note_stack_video_info": False,
        }
        return await self.private_graphql_query_request(
            friendly_name="InboxTrayRequestForUserQuery",
            root_field_name="xdt_get_inbox_tray_items",
            variables=variables,
            client_doc_id=client_doc_id,
            priority=priority,
        )

    async def chaining(self, user_id: str) -> dict:
        """Get suggested users for a target user_id.

        Hits Instagram's private ``discover/chaining/`` endpoint — the
        same surface the official app uses to render the "Suggested
        for you" carousel under a profile. Returns the raw payload so
        the caller can decide what shape it wants (typically passed
        straight into :meth:`fetch_suggestion_details` for the
        expanded form).

        Parameters
        ----------
        user_id: str
            Target user pk.

        Raises
        ------
        InvalidTargetUser
            Instagram refused chaining for this target ("Not eligible
            for chaining."). Common on locked-down / private accounts
            and recently-flagged users.
        """
        params = {
            "module": "profile",
            "target_id": str(user_id),
            "profile_chaining_check": "false",
            "eligible_for_threads_cta": "false",
        }
        try:
            return await self.private_request("discover/chaining/", params=params)
        except UnknownError as e:
            if str(e) == "Not eligible for chaining.":
                raise InvalidTargetUser("Not eligible for chaining.") from e
            raise

    async def fetch_suggestion_details(
        self,
        user_id: str,
        chained_ids: Union[str, List[Union[str, int]]],
    ) -> dict:
        """Fetch expanded details for chained suggestion ids.

        Companion to :meth:`chaining`. Pass either a comma-separated
        string or a list of user pks (typically the ``pk`` field of
        every entry in ``chaining()['users']``) and Instagram returns
        the same users with social-context fields filled in (mutual
        followers, verification, friendship state, etc.).

        Parameters
        ----------
        user_id: str
            Target user pk that produced the chained ids.
        chained_ids: Union[str, List[Union[str, int]]]
            Either a comma-separated string of user pks (IG-native
            shape) or a Python list of pks. Lists are joined with
            ``,`` internally; ints are coerced to str.
        """
        if isinstance(chained_ids, (list, tuple)):
            chained_ids = ",".join(str(x) for x in chained_ids)
        params = {
            "target_id": str(user_id),
            "chained_ids": chained_ids,
            "include_social_context": "1",
        }
        return await self.private_request(
            "discover/fetch_suggestion_details/",
            params=params,
        )

    async def user_suggested_profiles(self, user_id: str, expand_suggestion: bool = False) -> dict:
        """Get suggested profiles ("Suggested for you") for a target user_id.

        Convenience wrapper over :meth:`chaining` and
        :meth:`fetch_suggestion_details`. By default it returns the raw
        ``chaining`` payload; with ``expand_suggestion=True`` it feeds the
        chained pks back into ``fetch_suggestion_details`` and returns the
        social-context-rich payload instead.

        Parameters
        ----------
        user_id: str
            Target user pk whose suggested profiles to fetch.
        expand_suggestion: bool, optional
            When ``True``, return the expanded ``fetch_suggestion_details``
            payload. Falls back to the ``chaining`` payload when the target
            has no chained users. Defaults to ``False``.

        Returns
        -------
        dict
            Raw ``discover/chaining/`` response, or the
            ``discover/fetch_suggestion_details/`` response when
            ``expand_suggestion`` is ``True`` and chained users exist
            (currently keyed by ``items`` in app responses).

        Raises
        ------
        InvalidTargetUser
            Instagram refused chaining for this target ("Not eligible
            for chaining."). Common on locked-down / private accounts.
        """
        chained = await self.chaining(user_id)
        if not expand_suggestion:
            return chained
        chained_ids = ",".join(str(user["pk"]) for user in chained.get("users", []) if user.get("pk"))
        if not chained_ids:
            return chained
        return await self.fetch_suggestion_details(user_id, chained_ids)

    @staticmethod
    def _serialize_address_book_contacts(contacts: List[Union[AddressBookContact, dict]]) -> List[dict]:
        return [
            contact.model_dump(exclude_none=True) if isinstance(contact, AddressBookContact) else contact
            for contact in contacts
        ]

    @staticmethod
    def _serialize_address_book_include(include: Union[str, Sequence[str]]) -> str:
        if isinstance(include, str):
            return include
        return ",".join(str(field) for field in include)

    async def address_book_link(
        self,
        contacts: List[Union[AddressBookContact, dict]],
        include: Union[str, Sequence[str]] = ADDRESS_BOOK_DEFAULT_INCLUDE,
    ) -> dict:
        """
        Upload/link address book contacts and return Instagram's raw suggestions response.

        Parameters
        ----------
        contacts: List[AddressBookContact | dict]
            Address book contacts as typed objects, or raw dictionaries in
            Instagram's mobile payload shape, for example
            ``{"phone_numbers": [{"phone_number": "+15555550123"}],
            "email_addresses": [], "first_name": "Test", "last_name": "Contact"}``.
        include: Sequence[str] | str, optional
            Optional response fields requested from Instagram. Defaults to
            ``("extra_display_name", "thumbnails")``.

        Returns
        -------
        dict
            Raw ``address_book/link/`` response, usually containing suggested users
            when Instagram matches uploaded contacts.
        """
        include_value = self._serialize_address_book_include(include)
        data = {
            "contacts": json.dumps(self._serialize_address_book_contacts(contacts), separators=(",", ":")),
            "_uuid": self.uuid,
        }
        if self.user_id:
            data["_uid"] = str(self.user_id)
        return await self.private_request(
            "address_book/link/",
            data=data,
            params={"include": include_value} if include_value else None,
        )

    async def address_book_unlink(self) -> dict:
        """
        Disconnect the uploaded address book from the current account.

        Returns
        -------
        dict
            Raw ``address_book/unlink/`` response.
        """
        return await self.private_request(
            "address_book/unlink/",
            data={"_uuid": self.uuid},
        )

    async def discover_recommended_accounts_for_category_v1(self, user_id: str) -> dict:
        """
        Get business-category-similar accounts for a target user.

        Two-step call:

        1. Fetch the target's profile via :meth:`user_stream_by_id_v1`
           to extract ``category_id`` from the streamed payload.
        2. Hit ``GET /discover/recommended_accounts_for_category/``
           with that ``category_id`` to get IG's "similar businesses"
           recommendations for that category.

        Parameters
        ----------
        user_id: str
            Target user pk.

        Returns
        -------
        dict
            Raw recommended-accounts payload. ``category_id`` will be
            ``None`` if the target has no business category — IG
            still returns a payload (typically with empty ``users``)
            in that case.
        """
        user_info = await self.user_stream_by_id_v1(user_id)
        category_id = next(
            (
                cid
                for row in user_info.get("stream_rows", [])
                if (cid := row.get("user", {}).get("category_id")) is not None
            ),
            None,
        )
        return await self.private_request(
            "discover/recommended_accounts_for_category/",
            params={"target_id": user_id, "category_id": category_id},
        )

    async def user_related_profiles_gql(self, user_id: str) -> List[UserShort]:
        """
        Get related profiles for a target user via the public GraphQL
        ``edge_chaining`` field.

        Hits the legacy ``query_hash="ad99dd9d3646cc3c0dda65debcd266a7"``
        — IG has been gating this query_hash more aggressively over
        time; it may raise ``ClientGraphqlError`` on logged-out or
        rate-limited callers. For a more reliable mobile-app-style
        suggestion list, use :meth:`chaining` (private API).

        Parameters
        ----------
        user_id: str
            Target user pk.

        Returns
        -------
        List[UserShort]
            Related profiles. Empty list if IG returned no edges.

        Raises
        ------
        UserNotFound
            GraphQL response had no ``user`` block.
        RelatedProfileRequired
            Empty result and the caller had ``self.num_retry`` set
            below 4 (opt-in retry signal — set ``client.num_retry``
            yourself to enable).
        """
        variables = {
            "user_id": str(user_id),
            "include_chaining": True,
        }
        data = await self.public_graphql_request(variables, query_hash="ad99dd9d3646cc3c0dda65debcd266a7")
        if not data.get("user"):
            raise UserNotFound("User not found")
        edges = json_value(data, "user", "edge_chaining", "edges", default=[])
        res = [extract_user_short(e["node"]) for e in edges if "node" in e]
        if not res and getattr(self, "num_retry", None) is not None and self.num_retry < 4:
            raise RelatedProfileRequired
        return res
