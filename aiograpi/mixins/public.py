import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import parse_qs, urlparse

import orjson

from aiograpi import httpx_ext
from aiograpi.exceptions import (
    AboutUsError,
    AccountSuspended,
    ClientBadRequestError,
    ClientConnectionError,
    ClientError,
    ClientForbiddenError,
    ClientGraphqlError,
    ClientIncompleteReadError,
    ClientJSONDecodeError,
    ClientLoginRequired,
    ClientNotFoundError,
    ClientThrottledError,
    ClientUnauthorizedError,
    IsRegulatedC18Error,
    TermsAccept,
    TermsUnblock,
)
from aiograpi.mixins.base import ClientMixin
from aiograpi.utils.logging import redact_url_for_log
from aiograpi.utils.timing import random_delay

PublicTransport = Literal["requests", "curl"]
PUBLIC_WEB_APP_ID = "936619743392459"
PUBLIC_WEB_ASBD_ID = "359341"
PUBLIC_WEB_RELAY_CONTEXT_TTL_SECONDS = 15 * 60
PUBLIC_WEB_RELAY_PROFILE_CONTROLLER = "XPolarisProfileController"
PUBLIC_WEB_RELAY_PROFILE_ROUTE = "comet.igweb.PolarisLoggedOutDesktopWWWProfileRoute"


class PublicRequestMixin(ClientMixin):
    public_requests_count = 0
    PUBLIC_API_URL = "https://www.instagram.com/"
    GRAPHQL_PUBLIC_API_URL = "https://www.instagram.com/graphql/query/"
    GRAPHQL_PUBLIC_WEB_API_URL = "https://www.instagram.com/api/graphql"
    last_public_response = None
    last_public_json = {}
    public_request_logger = logging.getLogger("public_request")
    public_request_retries_count = 3
    public_request_retries_timeout = 2
    last_response_ts = 0
    public_transport = "requests"
    public_transport_impersonate = "chrome136"
    public_user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_6) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/11.1.2 Safari/605.1.15"
    )
    public_curl_user_agents = {
        "chrome136": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
    }
    public_accept_language = "en-US"

    def __init__(self, *args, **kwargs):
        self._public_web_relay_context: Optional[Dict[str, Any]] = None
        self._public_web_relay_request_sequence = 0
        self.last_public_web_relay_request_count = 0
        self.last_public_web_relay_response_bytes = 0
        self.last_public_web_relay_bootstrap_bytes = 0
        self.public_transport = self._normalize_public_transport(
            kwargs.pop("public_transport", getattr(self, "public_transport", self.public_transport))
        )
        self.public_transport_impersonate = kwargs.pop(
            "public_transport_impersonate",
            getattr(self, "public_transport_impersonate", self.public_transport_impersonate),
        )
        self.public_user_agent = kwargs.pop(
            "public_user_agent",
            self._default_public_user_agent(self.public_transport, self.public_transport_impersonate),
        )
        self.public_accept_language = kwargs.pop(
            "public_accept_language", getattr(self, "public_accept_language", self.public_accept_language)
        )
        self.public = self._build_public_session()
        self.public.headers.update(
            {
                "Connection": "Keep-Alive",
                "Accept": "*/*",
                "Accept-Encoding": "gzip,deflate",
                "Accept-Language": self.public_accept_language,
                "User-Agent": self.public_user_agent,
            }
        )
        self.public_request_retries_count = kwargs.pop(
            "public_request_retries_count",
            getattr(
                self,
                "public_request_retries_count",
                self.public_request_retries_count,
            ),
        )
        self.public_request_retries_timeout = kwargs.pop(
            "public_request_retries_timeout",
            getattr(
                self,
                "public_request_retries_timeout",
                self.public_request_retries_timeout,
            ),
        )
        super().__init__(*args, **kwargs)

    @classmethod
    def _normalize_public_transport(cls, public_transport: Optional[PublicTransport]) -> PublicTransport:
        public_transport = public_transport or "requests"
        if public_transport not in {"requests", "curl"}:
            raise ValueError("public_transport must be 'requests' or 'curl'")
        return public_transport

    @classmethod
    def _default_public_user_agent(cls, public_transport: PublicTransport, impersonate: str) -> str:
        if public_transport == "curl":
            return cls.public_curl_user_agents.get(impersonate, cls.public_curl_user_agents["chrome136"])
        return cls.public_user_agent

    def _build_public_session(self, verify=None):
        verify = getattr(self, "tls_verify", True) if verify is None else verify
        if self.public_transport == "curl":
            return httpx_ext.CurlSession(verify=verify, impersonate=self.public_transport_impersonate)
        return httpx_ext.Session(verify=verify)

    def _configure_public_transport(self):
        old_public = getattr(self, "public", None)
        old_proxy = getattr(old_public, "proxy", None)
        old_verify = getattr(old_public, "verify", True)
        old_headers = dict(getattr(old_public, "headers", {}))
        old_cookies = old_public.cookies_dict() if old_public is not None else {}

        self.public = self._build_public_session(verify=old_verify)
        self.public.proxy = old_proxy
        self.public.headers.update(old_headers)
        if old_cookies:
            self.public.set_cookies(old_cookies)
        self.clear_public_web_relay_context()

    def clear_public_web_relay_context(self) -> None:
        """Discard short-lived anonymous Relay metadata after transport changes or failures."""

        self._public_web_relay_context = None
        self._public_web_relay_request_sequence = 0

    @property
    def public_web_relay_context_ready(self) -> bool:
        context = self._public_web_relay_context
        if not isinstance(context, dict):
            return False
        fetched_at = context.get("fetched_at")
        return isinstance(fetched_at, (int, float)) and (
            time.monotonic() - fetched_at < PUBLIC_WEB_RELAY_CONTEXT_TTL_SECONDS
        )

    async def public_head(self, url: str, follow_redirects: bool = False):
        """
        Issue a ``HEAD`` request through the public session — useful
        for resolving short-link redirects without downloading the
        body (e.g. ``instagram.com/share/...`` link expansion).

        Bypasses :meth:`public_request`'s GET/POST machinery and goes
        straight through ``httpx_ext.request`` so the per-call
        ``follow_redirects`` flag actually takes effect (the Session
        wrapper filters falsy kwargs and would drop
        ``follow_redirects=False``).

        Parameters
        ----------
        url: str
            Absolute URL.
        follow_redirects: bool, default False
            Whether httpx should follow 3xx responses. Default
            ``False`` means callers can read ``response.headers["location"]``
            to inspect the redirect target without actually fetching it.

        Returns
        -------
        httpx.Response
            The raw response. Status code typically 200 / 301 / 302 /
            307 / 308.
        """
        self.public_requests_count += 1
        if self.public_transport == "curl":
            return await self.public.head(url, headers=self.public.headers, allow_redirects=follow_redirects)
        return await httpx_ext.request(
            "HEAD",
            url,
            proxy=self.public.proxy,
            verify=self.public.verify,
            follow_redirects=follow_redirects,
            headers=self.public.headers,
        )

    async def public_request(
        self,
        url,
        data=None,
        params=None,
        headers=None,
        update_headers=None,
        return_json=False,
        retries_count=None,
        retries_timeout=None,
    ):
        kwargs = dict(
            data=data,
            params=params,
            headers=headers,
            return_json=return_json,
        )
        retries_count = self.public_request_retries_count if retries_count is None else retries_count
        retries_timeout = self.public_request_retries_timeout if retries_timeout is None else retries_timeout
        assert retries_count <= 10, "Retries count is too high"
        assert retries_timeout <= 600, "Retries timeout is too high"
        for iteration in range(retries_count):
            try:
                if self.delay_range:
                    await random_delay(delay_range=self.delay_range)
                return await self._send_public_request(url, update_headers=update_headers, **kwargs)
            except (
                ClientLoginRequired,
                ClientNotFoundError,
                ClientBadRequestError,
            ) as e:
                raise e  # Stop retries
            # except JSONDecodeError as e:
            #     raise ClientJSONDecodeError(e, respones=self.last_public_response)
            except ClientError as e:
                msg = str(e)
                if all(
                    (
                        isinstance(e, ClientConnectionError),
                        "SOCKSHTTPSConnectionPool" in msg,
                        "Max retries exceeded with url" in msg,
                        "Failed to establish a new connection" in msg,
                    )
                ):
                    raise e
                if retries_count > iteration + 1:
                    await asyncio.sleep(retries_timeout)
                else:
                    raise e
                continue

    async def _send_public_request(
        self,
        url,
        data=None,
        params=None,
        headers=None,
        return_json=False,
        update_headers=None,
    ):
        self.last_public_response = None
        self.public_requests_count += 1
        # Two header modes:
        #   update_headers in (None, True): merge into the session (legacy
        #     behavior — persists across subsequent requests).
        #   update_headers is False: pass per-request only, no mutation.
        per_request_headers = None
        if headers:
            if update_headers in [None, True]:
                self.public.headers.update(headers)
            else:
                per_request_headers = headers
        if self.last_response_ts and (time.time() - self.last_response_ts) < 1.0:
            await asyncio.sleep(1.0)
        try:
            if data is not None:
                response = await self.public.post(url, data=data, params=params, headers=per_request_headers)
            else:
                response = await self.public.get(url, params=params, headers=per_request_headers)
            safe_url = redact_url_for_log(response.url)
            self.public_request_logger.debug("public_request %s: %s", response.status_code, safe_url)
            self.public_request_logger.info(
                "[%s] [%s] %s %s",
                "proxy" if self.public.proxy else "direct",
                response.status_code,
                "POST" if data else "GET",
                safe_url,
            )
            self.last_public_response = response
            response.raise_for_status()
            if return_json:
                self.last_public_json = response.json()
                return self.last_public_json
            return response.text

        except orjson.JSONDecodeError as e:
            url = redact_url_for_log(response.url)
            if "/login/" in url:
                raise ClientLoginRequired(e, response=response)
            elif "/challenge/" in url:
                raise ClientLoginRequired(e, response=response)
            elif "/suspended/" in url:
                raise AccountSuspended(e, response=response)
            elif "/terms/unblock" in url:
                raise TermsUnblock(e, response=response)
            elif "/terms/accept" in url:
                raise TermsAccept(e, response=response)
            elif "/about-us" in url:
                raise AboutUsError(e, response=response)

            self.public_request_logger.error(
                "Status %s: JSONDecodeError in public_request (url=%s response_bytes=%s)",
                response.status_code,
                url,
                len(response.content),
            )
            raise ClientJSONDecodeError(
                "JSONDecodeError {0!s} while opening {1!s}".format(e, url),
                response=response,
            )
        except httpx_ext.HTTPError as e:
            match getattr(self.last_public_response, "status_code", None):
                case 401:
                    exc = ClientUnauthorizedError
                case 403:
                    exc = ClientForbiddenError
                case 400:
                    exc = ClientBadRequestError
                case 429:
                    exc = ClientThrottledError
                case 404:
                    exc = ClientNotFoundError
                case 500:
                    if "Oops, an error occurred" in self.last_public_response.text:
                        exc = IsRegulatedC18Error
                case _:
                    exc = ClientError
            raise exc(e, response=self.last_public_response)
        except (httpx_ext.ConnectError, httpx_ext.ReadError) as e:
            raise ClientConnectionError("{} {}".format(e.__class__.__name__, str(e)))
        finally:
            self.last_response_ts = time.time()

    def _expected_content_length(self, response) -> Optional[int]:
        content_length = response.headers.get("Content-Length")
        if not content_length:
            return None
        try:
            return int(content_length)
        except (TypeError, ValueError):
            return None

    def _raise_for_incomplete_download(self, actual_length: int, expected_length: Optional[int], source: str) -> None:
        if expected_length is None or actual_length == expected_length:
            return
        raise ClientIncompleteReadError(
            f"Broken file {source} (Content-length={expected_length}, but file length={actual_length})"
        )

    def _download_response_to_path(self, response, path: Path) -> Path:
        path = Path(path)
        try:
            content = response.read()
            with open(path, "wb") as f:
                f.write(content)
            self._raise_for_incomplete_download(
                path.stat().st_size,
                self._expected_content_length(response),
                f'"{path}"',
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path.resolve()

    def _download_response_bytes(self, response, url: str) -> bytes:
        content = response.content
        self._raise_for_incomplete_download(
            len(content),
            self._expected_content_length(response),
            f'from url "{url}"',
        )
        return content

    async def public_graphql_request(
        self,
        variables,
        query_hash=None,
        query_id=None,
        data=None,
        params=None,
        headers=None,
        retries_count=None,
    ):
        assert query_id or query_hash, "Must provide valid one of: query_id, query_hash"
        default_params = {"variables": json.dumps(variables, separators=(",", ":"))}
        if query_id:
            default_params["query_id"] = query_id
        if query_hash:
            default_params["query_hash"] = query_hash
        if params:
            params.update(default_params)
        else:
            params = default_params

        try:
            body_json = await self.public_request(
                self.GRAPHQL_PUBLIC_API_URL,
                data=data,
                params=params,
                headers=headers,
                return_json=True,
                retries_count=retries_count,
            )

            if body_json.get("status", None) != "ok":
                raise ClientGraphqlError(
                    "Unexpected status '{}' in response. Message: '{}'".format(
                        body_json.get("status", None), body_json.get("message", None)
                    ),
                    response=body_json,
                )

            if "data" not in body_json:
                errors = body_json.get("errors") or []
                summary = errors[0].get("summary") if errors else None
                description = errors[0].get("description") if errors else None
                raise ClientGraphqlError(
                    "Missing 'data' in GraphQL response. Summary: '{}'. Description: '{}'".format(summary, description)
                )

            return body_json["data"]

        except ClientBadRequestError as e:
            message = None
            try:
                body_json = e.response.json()
                message = body_json.get("message", None)
            except orjson.JSONDecodeError:
                pass
            raise ClientGraphqlError("Error: '{}'. Message: '{}'".format(e, message), response=e.response)

    @staticmethod
    def _extract_public_lsd_token(html: str) -> Optional[str]:
        if not html:
            return None
        # The bootstrap payload is emitted as a compact array in some
        # deployments and with whitespace/newline separators in others. Keep
        # this parser scoped to the named LSD entry rather than searching for
        # arbitrary ``token`` keys in the document.
        match = re.search(
            r'\[\s*"LSD"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"\\]+)"',
            html,
        )
        return match.group(1) if match else None

    @staticmethod
    def _extract_public_web_relay_context(html: str) -> Dict[str, Any]:
        """Parse the bounded anonymous Relay context embedded in a profile page.

        Instagram rotates these values with each web deployment. Keeping them
        out of source avoids stale hard-coded revisions while strict shape
        checks prevent arbitrary page content from becoming request metadata.
        Tokens stay only in memory and are never logged.
        """

        if not isinstance(html, str) or not html or len(html) > 5_000_000:
            raise ClientGraphqlError("Invalid public Relay bootstrap document")
        eqmc_match = re.search(
            r'<script[^>]*\bid\s*=\s*["\']__eqmc["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not eqmc_match:
            raise ClientGraphqlError("Public Relay bootstrap metadata is missing")
        try:
            eqmc = json.loads(eqmc_match.group(1))
        except (TypeError, ValueError) as error:
            raise ClientGraphqlError("Public Relay bootstrap metadata is invalid") from error

        site_match = re.search(r'\[\s*"SiteData"\s*,\s*\[\s*\]\s*,\s*', html)
        if not site_match:
            raise ClientGraphqlError("Public Relay site metadata is missing")
        try:
            site_data, _ = json.JSONDecoder().raw_decode(html[site_match.end() :])
        except (TypeError, ValueError) as error:
            raise ClientGraphqlError("Public Relay site metadata is invalid") from error
        if not isinstance(eqmc, dict) or not isinstance(site_data, dict):
            raise ClientGraphqlError("Public Relay bootstrap shape is invalid")

        controller = str(eqmc.get("s") or "")
        # Recent logged-out pages keep the LSD value in the named bootstrap
        # entry while ``__eqmc.l`` is null. Prefer the explicit ``l`` value
        # when present, then fall back to the scoped LSD entry. Both values
        # are validated below before becoming request metadata.
        lsd = str(eqmc.get("l") or "")
        if not lsd:
            lsd = str(PublicRequestMixin._extract_public_lsd_token(html) or "")
        hsi = str(eqmc.get("e") or "")
        query = parse_qs(urlparse(str(eqmc.get("u") or "")).query)
        jazoest = str((query.get("jazoest") or [""])[0])
        site_hsi = str(site_data.get("hsi") or "")
        haste_session = str(site_data.get("haste_session") or "")
        server_revision = str(site_data.get("server_revision") or "")
        spin_revision = str(site_data.get("__spin_r") or "")
        spin_timestamp = str(site_data.get("__spin_t") or "")
        spin_branch = str(site_data.get("__spin_b") or "")
        comet_request = str(site_data.get("comet_env") or "")
        device_pixel_ratio = str(site_data.get("pr") or "1")

        if controller != PUBLIC_WEB_RELAY_PROFILE_CONTROLLER:
            raise ClientGraphqlError("Unsupported public Relay controller")
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", lsd):
            raise ClientGraphqlError("Public Relay LSD token is invalid")
        if not re.fullmatch(r"[0-9]{8,30}", hsi) or hsi != site_hsi:
            raise ClientGraphqlError("Public Relay HSI metadata is invalid")
        if not re.fullmatch(r"[0-9]{4,20}", jazoest):
            raise ClientGraphqlError("Public Relay jazoest metadata is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", haste_session):
            raise ClientGraphqlError("Public Relay haste session is invalid")
        if not re.fullmatch(r"[0-9]{1,20}", server_revision):
            raise ClientGraphqlError("Public Relay server revision is invalid")
        if not re.fullmatch(r"[0-9]{1,20}", spin_revision):
            raise ClientGraphqlError("Public Relay spin revision is invalid")
        if not re.fullmatch(r"[0-9]{1,20}", spin_timestamp):
            raise ClientGraphqlError("Public Relay spin timestamp is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", spin_branch):
            raise ClientGraphqlError("Public Relay spin branch is invalid")
        if not re.fullmatch(r"[0-9]{1,3}", comet_request):
            raise ClientGraphqlError("Public Relay comet environment is invalid")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", device_pixel_ratio):
            device_pixel_ratio = "1"

        return {
            "lsd": lsd,
            "jazoest": jazoest,
            "hsi": hsi,
            "haste_session": haste_session,
            "server_revision": server_revision,
            "spin_revision": spin_revision,
            "spin_timestamp": spin_timestamp,
            "spin_branch": spin_branch,
            "comet_request": comet_request,
            "device_pixel_ratio": device_pixel_ratio,
            "fetched_at": time.monotonic(),
        }

    @staticmethod
    def _decode_public_web_relay_response(text: str) -> Dict[str, Any]:
        if not isinstance(text, str) or not text or len(text) > 10_000_000:
            raise ClientGraphqlError("Invalid public Relay response")
        stripped = text.lstrip()
        if stripped.startswith("for (;;);"):
            stripped = stripped[len("for (;;);") :].lstrip()
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError) as error:
            raise ClientGraphqlError("Invalid public Relay JSON response") from error
        if not isinstance(payload, dict):
            raise ClientGraphqlError("Invalid public Relay response shape")
        return payload

    @staticmethod
    def _relay_request_token(sequence: int) -> str:
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
        value = max(1, int(sequence))
        token = ""
        while value:
            value, remainder = divmod(value, 36)
            token = alphabet[remainder] + token
        return token

    async def public_web_relay_request(
        self,
        doc_id: str,
        variables: Dict[str, Any],
        *,
        referer: str,
        friendly_name: str,
        retries_count: int = 1,
        expected_request_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute one anonymous Instagram web Relay operation.

        A cold public session performs one profile-page bootstrap followed by
        one Relay POST. Warm sessions reuse the short-lived metadata and send
        only the POST. This method never retries provider traffic implicitly;
        durable callers must reserve and account for every HTTP attempt.
        """

        normalized_doc_id = str(doc_id or "").strip()
        normalized_friendly_name = str(friendly_name or "").strip()
        if expected_request_count not in {None, 1, 2}:
            raise ValueError("expected_request_count must be 1, 2, or None")
        if self.public_transport != "curl":
            raise ClientGraphqlError(
                "Anonymous public Relay requires the curl transport for a consistent browser fingerprint"
            )
        if not re.fullmatch(r"[0-9]{8,40}", normalized_doc_id):
            raise ValueError("doc_id must be an 8-40 digit value")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", normalized_friendly_name):
            raise ValueError("friendly_name contains unsupported characters")
        parsed_referer = urlparse(str(referer or "").strip())
        if parsed_referer.scheme != "https" or parsed_referer.netloc not in {
            "instagram.com",
            "www.instagram.com",
        }:
            raise ValueError("referer must be an Instagram HTTPS URL")
        cookies = self.public.cookies_dict()
        if cookies.get("sessionid") or cookies.get("ds_user_id"):
            raise ClientGraphqlError("Anonymous public Relay transport contains authenticated cookies")

        # Durable callers reserve provider requests before touching Instagram.
        # Pinning the expected count makes that reservation exact: a warm
        # one-request reservation can never silently bootstrap, while a cold
        # two-request reservation deliberately refreshes even if an old
        # context became visible between planning and execution.
        if expected_request_count == 2:
            self.clear_public_web_relay_context()
        context_ready = self.public_web_relay_context_ready
        if expected_request_count == 1 and not context_ready:
            raise ClientGraphqlError("Reserved warm Relay context is unavailable")

        self.last_public_web_relay_request_count = 0
        self.last_public_web_relay_response_bytes = 0
        self.last_public_web_relay_bootstrap_bytes = 0
        if not context_ready:
            self.last_public_web_relay_request_count += 1
            bootstrap_html = await self.public_request(
                referer,
                return_json=False,
                retries_count=retries_count,
            )
            bootstrap_response = self.last_public_response
            bootstrap_content = getattr(bootstrap_response, "content", b"")
            self.last_public_web_relay_bootstrap_bytes = (
                len(bootstrap_content) if isinstance(bootstrap_content, bytes) else len(bootstrap_html.encode())
            )
            self.last_public_web_relay_response_bytes += self.last_public_web_relay_bootstrap_bytes
            self._public_web_relay_context = self._extract_public_web_relay_context(bootstrap_html)

        context = self._public_web_relay_context
        if not isinstance(context, dict):
            raise ClientGraphqlError("Public Relay bootstrap context is unavailable")
        self._public_web_relay_request_sequence += 1
        data = {
            "av": "0",
            "__d": "www",
            "__user": "0",
            "__a": "1",
            "__req": self._relay_request_token(self._public_web_relay_request_sequence),
            "__hs": context["haste_session"],
            "dpr": context["device_pixel_ratio"],
            "__ccg": "EXCELLENT",
            "__rev": context["server_revision"],
            "__s": "",
            "__hsi": context["hsi"],
            "__dyn": "",
            "__csr": "",
            "__hblp": "",
            "__hsdp": "",
            "__sjsp": "",
            "__crn": PUBLIC_WEB_RELAY_PROFILE_ROUTE,
            "__comet_req": context["comet_request"],
            "lsd": context["lsd"],
            "jazoest": context["jazoest"],
            "__spin_r": context["spin_revision"],
            "__spin_b": context["spin_branch"],
            "__spin_t": context["spin_timestamp"],
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": normalized_friendly_name,
            "server_timestamps": "true",
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": normalized_doc_id,
        }
        headers = {
            "Accept": "*/*",
            "Accept-Language": self.public_accept_language,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.instagram.com",
            "Priority": "u=1, i",
            "Referer": referer,
            "Sec-Ch-Prefers-Color-Scheme": "light",
            "Sec-Ch-Ua": '"Chromium";v="136", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Model": '""',
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Ch-Ua-Platform-Version": '""',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": self.public_user_agent,
            "X-ASBD-ID": PUBLIC_WEB_ASBD_ID,
            "X-FB-Friendly-Name": normalized_friendly_name,
            "X-FB-LSD": context["lsd"],
            "X-IG-App-ID": PUBLIC_WEB_APP_ID,
            "X-IG-Max-Touch-Points": "0",
            "X-Requested-With": "XMLHttpRequest",
        }
        csrftoken = str(self.public.cookies_dict().get("csrftoken") or "").strip()
        if csrftoken:
            headers["X-CSRFToken"] = csrftoken

        relay_bytes_accounted = False
        try:
            self.last_public_web_relay_request_count += 1
            response_text = await self.public_request(
                self.GRAPHQL_PUBLIC_WEB_API_URL,
                data=data,
                headers=headers,
                update_headers=False,
                return_json=False,
                retries_count=retries_count,
            )
            relay_content = getattr(self.last_public_response, "content", b"")
            relay_bytes = len(relay_content) if isinstance(relay_content, bytes) else len(response_text.encode())
            self.last_public_web_relay_response_bytes += relay_bytes
            relay_bytes_accounted = True
            body_json = self._decode_public_web_relay_response(response_text)
            if body_json.get("errors") or body_json.get("data") is None:
                self.clear_public_web_relay_context()
                raise ClientGraphqlError(
                    "Public Relay operation returned no data",
                    response=self.last_public_response,
                )
            return body_json["data"]
        except Exception:
            # HTTP failures still consumed provider bandwidth. Capture it when
            # the transport made a response available, without double-counting
            # a decoded Relay error that was already measured above.
            if not relay_bytes_accounted:
                failed_content = getattr(self.last_public_response, "content", b"")
                if isinstance(failed_content, bytes):
                    self.last_public_web_relay_response_bytes += len(failed_content)
            # A stale web deployment context must not survive into a durable
            # retry. The next caller will explicitly budget a fresh bootstrap.
            self.clear_public_web_relay_context()
            raise

    async def public_doc_id_graphql_request(
        self,
        doc_id: str,
        variables: Dict[str, Any],
        referer: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
        include_lsd: bool = False,
        retries_count: Optional[int] = None,
        friendly_name: Optional[str] = None,
        web_headers: bool = False,
    ) -> Dict[str, Any]:
        """
        POST a doc_id-based GraphQL query to Instagram's public web endpoints.

        Newer Instagram web GraphQL endpoints use ``doc_id`` instead of the
        legacy ``query_hash`` / ``query_id`` scheme. Returns the parsed
        ``data`` payload.

        Parameters
        ----------
        doc_id: str
            doc_id of the registered query (e.g. "25980296051578533").
        variables: dict
            Query variables — will be JSON-encoded compactly into the
            ``variables`` form field.
        referer: str, optional
            Value for the ``Referer`` request header.
        headers: dict, optional
            Extra request headers merged on top of the public session's.
        retries_count: int, optional
            Maximum transport attempts. Durable callers should pass ``1``
            and own retry policy outside the client.
        friendly_name: str, optional
            Registered Relay operation name. When supplied it is sent in both
            the request headers and form body, matching Instagram web Relay.
        web_headers: bool, optional
            Use the browser GraphQL request envelope without performing an
            additional LSD/bootstrap request. This keeps authenticated
            durable calls to exactly one HTTP attempt.
        """
        normalized_doc_id = str(doc_id or "").strip()
        if not re.fullmatch(r"[0-9]{8,40}", normalized_doc_id):
            raise ValueError("doc_id must be an 8-40 digit value")
        normalized_friendly_name = str(friendly_name or "").strip()
        if normalized_friendly_name and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,127}",
            normalized_friendly_name,
        ):
            raise ValueError("friendly_name contains unsupported characters")
        data = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": normalized_doc_id,
            "server_timestamps": "true",
        }
        if normalized_friendly_name:
            data.update(
                {
                    "fb_api_caller_class": "RelayModern",
                    "fb_api_req_friendly_name": normalized_friendly_name,
                }
            )
        inject_sessionid = getattr(self, "inject_sessionid_to_public", None)
        if inject_sessionid:
            inject_sessionid()
        if web_headers:
            actor_id = str(self.public.cookies_dict().get("ds_user_id") or "").strip()
            if actor_id.isdigit():
                data["av"] = actor_id
        # RelayModern persisted operations are served by Instagram's web
        # GraphQL endpoint.  The legacy ``/graphql/query/`` endpoint still
        # backs query-hash and older doc-id callers, so only select the modern
        # route when the caller explicitly requests a browser envelope.
        query_url = url or (
            self.GRAPHQL_PUBLIC_WEB_API_URL if include_lsd or web_headers else self.GRAPHQL_PUBLIC_API_URL
        )
        referer_url = referer or "https://www.instagram.com/"
        lsd = None
        if include_lsd:
            html = await self.public_request(referer_url, return_json=False)
            lsd = self._extract_public_lsd_token(html)
            if lsd:
                data["lsd"] = lsd
        merged_headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": referer_url,
            "User-Agent": (
                "Instagram 273.0.0.16.70 (iPhone15,2; iOS 17_5_1; en_US; en-US; scale=3.00; 1290x2796; 470085518)"
            ),
        }
        if include_lsd or web_headers:
            merged_headers.update(
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.instagram.com",
                    "User-Agent": self.public_user_agent,
                    "X-ASBD-ID": PUBLIC_WEB_ASBD_ID,
                    "X-IG-App-ID": PUBLIC_WEB_APP_ID,
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
            if lsd:
                merged_headers["X-FB-LSD"] = lsd
        if normalized_friendly_name:
            merged_headers["X-FB-Friendly-Name"] = normalized_friendly_name
        csrftoken = self.public.cookies_dict().get("csrftoken")
        if not csrftoken and web_headers:
            candidate = str(getattr(self, "token", "") or "").strip()
            if candidate:
                self.public.set_cookies({"csrftoken": candidate})
                csrftoken = candidate
        if csrftoken:
            merged_headers["X-CSRFToken"] = csrftoken
        if headers:
            merged_headers.update(headers)
        body_json = await self.public_request(
            query_url,
            data=data,
            headers=merged_headers,
            update_headers=False,
            return_json=True,
            retries_count=retries_count,
        )
        if body_json.get("data") is None:
            raise ClientGraphqlError(
                "Missing 'data' in doc_id GraphQL response",
                response=body_json,
            )
        return body_json["data"]


class TopSearchesPublicMixin(ClientMixin):
    async def top_search(self, query):
        """Anonymous IG search request"""
        url = "https://www.instagram.com/web/search/topsearch/"
        params = {
            "context": "blended",
            "query": query,
            "rank_token": 0.7763938004511706,
            "include_reel": "true",
        }
        return await self.public_request(url, params=params, return_json=True)


class ProfilePublicMixin(ClientMixin):
    async def location_feed(self, location_id, count=16, end_cursor=None):
        if count > 50:
            raise ValueError("Count cannot be greater than 50")
        variables = {
            "id": location_id,
            "first": int(count),
        }
        if end_cursor:
            variables["after"] = end_cursor
        data = await self.public_graphql_request(variables, query_hash="1b84447a4d8b6d6d0426fefb34514485")
        return data["location"]

    async def profile_related_info(self, profile_id):
        variables = {
            "user_id": profile_id,
            "include_chaining": True,
            "include_reel": True,
            "include_suggested_users": True,
            "include_logged_out_extras": True,
            "include_highlight_reels": True,
            "include_related_profiles": True,
        }
        data = await self.public_graphql_request(variables, query_hash="e74d51c10ecc0fe6250a295b9bb9db74")
        return data["user"]
