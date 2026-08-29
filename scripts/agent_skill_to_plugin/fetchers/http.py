"""Bounded HTTPS downloads with redirect and SSRF validation.

The fetcher intentionally uses only the Python standard library.  Every URL,
including every redirect target, is validated before a request is made.  DNS
answers must all be globally routable; a mixed public/private answer is
rejected rather than choosing the apparently safe address.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import socket
import tempfile
from typing import Callable, Iterable
import urllib.error
import urllib.parse
import urllib.request

from ..errors import SkillToPluginError
from ..limits import DEFAULT_TIMEOUT_SECONDS, MAX_HTTP_BYTES, MAX_REDIRECTS
from ..utils import sanitize_text, validate_url_credentials


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("168.63.129.16"),  # Azure platform virtual IP
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure IMDS
        ipaddress.ip_address("169.254.170.2"),  # AWS container credentials
    }
)


@dataclass(frozen=True)
class HttpFetchResult:
    """Result of one completed, bounded HTTPS download."""

    url: str
    path: Path
    sha256: str
    size: int
    content_type: str | None = None
    redirects: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ResolvedHttpsUrl:
    """An HTTPS URL bound to the public addresses that were validated."""

    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Expose redirects to the validation loop instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _default_resolver(host: str, port: int) -> Iterable[tuple]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _address_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # ``is_global`` rejects loopback, private, link-local, multicast,
    # unspecified, documentation, reserved, and shared address space.  Keep an
    # explicit metadata list because some provider platform addresses are
    # classified as globally routable by the stdlib tables.
    return address.is_global and address not in _METADATA_ADDRESSES


def _validate_and_resolve_public_https_url(
    value: str,
    *,
    resolver: Callable[[str, int], Iterable[tuple]] | None = None,
) -> _ResolvedHttpsUrl:
    """Validate one URL and retain the exact public IPs for the connection."""

    validate_url_credentials(value)
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise SkillToPluginError(
            "The download URL is malformed.",
            code="security_rejected",
            details={"url": sanitize_text(value)},
        ) from exc
    if parsed.scheme.casefold() != "https":
        raise SkillToPluginError(
            "Only HTTPS downloads are allowed.",
            code="security_rejected",
            details={"url": sanitize_text(value)},
        )
    if not parsed.hostname:
        raise SkillToPluginError(
            "The HTTPS URL does not contain a hostname.",
            code="security_rejected",
            details={"url": sanitize_text(value)},
        )
    try:
        host = parsed.hostname.encode("idna").decode("ascii").rstrip(".").casefold()
        port = parsed.port or 443
    except (UnicodeError, ValueError) as exc:
        raise SkillToPluginError(
            "The HTTPS URL contains an invalid hostname or port.",
            code="security_rejected",
            details={"url": sanitize_text(value)},
        ) from exc
    if not host or host == "localhost" or host.endswith(".localhost") or host in _METADATA_HOSTS:
        raise SkillToPluginError(
            "Localhost and cloud metadata hosts are not valid remote sources.",
            code="security_rejected",
            details={"host": host},
        )

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        try:
            addresses.add(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            resolve = resolver or _default_resolver
            for answer in resolve(host, port):
                sockaddr = answer[4]
                if not sockaddr:
                    continue
                addresses.add(ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]))
    except (OSError, ValueError) as exc:
        raise SkillToPluginError(
            "Could not resolve the HTTPS source hostname.",
            code="network_failed",
            details={"host": host},
        ) from exc
    if not addresses:
        raise SkillToPluginError(
            "The HTTPS source hostname resolved to no usable addresses.",
            code="network_failed",
            details={"host": host},
        )
    unsafe = sorted(str(address) for address in addresses if not _address_is_public(address))
    if unsafe:
        raise SkillToPluginError(
            "The HTTPS source resolves to a non-public or metadata address.",
            code="security_rejected",
            details={"host": host, "addresses": unsafe},
        )

    # Keep the original, correctly escaped path/query, but canonicalize the
    # authority to the exact IDNA hostname that was validated.
    username_free_netloc = host
    if ":" in host and not host.startswith("["):
        username_free_netloc = f"[{host}]"
    if port != 443:
        username_free_netloc = f"{username_free_netloc}:{port}"
    normalized = urllib.parse.urlunsplit(
        ("https", username_free_netloc, parsed.path or "/", parsed.query, "")
    )
    return _ResolvedHttpsUrl(
        url=normalized,
        hostname=host,
        port=port,
        addresses=tuple(sorted(str(address) for address in addresses)),
    )


def validate_public_https_url(
    value: str,
    *,
    resolver: Callable[[str, int], Iterable[tuple]] | None = None,
) -> str:
    """Validate an HTTPS URL and all current DNS answers.

    The returned URL has its fragment removed because fragments are not sent
    in HTTP requests.  Credentials and credential-like query parameters are
    rejected by the shared URL policy before DNS is consulted.
    """

    return _validate_and_resolve_public_https_url(value, resolver=resolver).url


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects only to prevalidated numeric IPs."""

    def __init__(
        self,
        host: str,
        *,
        pinned_addresses: tuple[str, ...],
        server_hostname: str,
        **kwargs,
    ) -> None:
        super().__init__(host, **kwargs)
        self._pinned_addresses = pinned_addresses
        self._validated_server_hostname = server_hostname

    def connect(self) -> None:
        if self._tunnel_host:
            raise OSError("HTTP CONNECT tunnels are not supported by the pinned downloader")
        last_error: OSError | None = None
        sock = None
        for address in self._pinned_addresses:
            try:
                # `address` is a normalized numeric IP from ipaddress, so this
                # cannot trigger a second attacker-controlled hostname lookup.
                sock = socket.create_connection(
                    (address, self.port),
                    self.timeout,
                    self.source_address,
                )
                break
            except OSError as exc:
                last_error = exc
        if sock is None:
            raise last_error or OSError("No validated HTTPS address was available")
        try:
            self.sock = self._context.wrap_socket(
                sock,
                server_hostname=self._validated_server_hostname,
            )
        except BaseException:
            sock.close()
            raise


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, resolved: _ResolvedHttpsUrl) -> None:
        super().__init__()
        self._resolved = resolved

    def https_open(self, request):  # type: ignore[no-untyped-def]
        def connection_factory(host: str, **kwargs):  # type: ignore[no-untyped-def]
            return _PinnedHTTPSConnection(
                host,
                pinned_addresses=self._resolved.addresses,
                server_hostname=self._resolved.hostname,
                **kwargs,
            )

        return self.do_open(connection_factory, request, context=self._context)


def _pinned_opener(resolved: _ResolvedHttpsUrl) -> urllib.request.OpenerDirector:
    # Environment proxy settings would move the TCP connection away from the
    # validated origin and reintroduce an unpinned trust boundary. Callers that
    # require a proxy must provide a separately audited transport instead.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
        _PinnedHTTPSHandler(resolved),
    )


class HttpFetcher:
    """Download public HTTPS resources without trusting redirects or headers."""

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = MAX_HTTP_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        resolver: Callable[[str, int], Iterable[tuple]] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        user_agent: str = "Agent-Skill-to-Plugin/0.5",
    ) -> None:
        if timeout_seconds <= 0 or max_bytes <= 0 or max_redirects < 0:
            raise ValueError("HTTP timeout and size must be positive; redirects cannot be negative")
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.resolver = resolver
        # An injected opener is a test seam. Production callers use the pinned
        # transport created per validated URL below.
        self.opener = opener
        self.user_agent = user_agent

    def fetch(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int | None = None,
    ) -> HttpFetchResult:
        """Download *url* atomically to the new or replaceable file path."""

        limit = self.max_bytes if max_bytes is None else max_bytes
        if limit <= 0 or limit > self.max_bytes:
            raise ValueError("The per-request byte limit must be within the fetcher limit")
        resolved = _validate_and_resolve_public_https_url(url, resolver=self.resolver)
        current = resolved.url
        redirects: list[str] = []

        destination = Path(destination)
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise SkillToPluginError(
                "The HTTP destination must be a regular file path.",
                code="output_conflict",
                details={"destination": str(destination)},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".download", dir=destination.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            while True:
                request = urllib.request.Request(
                    current,
                    headers={
                        "Accept": "application/octet-stream, application/json;q=0.9, */*;q=0.1",
                        "User-Agent": self.user_agent,
                    },
                    method="GET",
                )
                response = None
                try:
                    opener = self.opener or _pinned_opener(resolved)
                    response = opener.open(request, timeout=self.timeout_seconds)
                except urllib.error.HTTPError as exc:
                    if exc.code in _REDIRECT_STATUSES:
                        location = exc.headers.get("Location")
                        exc.close()
                        if not location:
                            raise SkillToPluginError(
                                "The HTTPS source returned a redirect without a Location header.",
                                code="network_failed",
                                details={"status": exc.code},
                            )
                        if len(redirects) >= self.max_redirects:
                            raise SkillToPluginError(
                                "The HTTPS source exceeded the redirect limit.",
                                code="network_failed",
                                details={"max_redirects": self.max_redirects},
                            )
                        target = urllib.parse.urljoin(current, location)
                        resolved = _validate_and_resolve_public_https_url(target, resolver=self.resolver)
                        current = resolved.url
                        redirects.append(current)
                        continue
                    code = "authentication_failed" if exc.code in {401, 403} else "network_failed"
                    status = exc.code
                    exc.close()
                    raise SkillToPluginError(
                        "The HTTPS source returned an error response.",
                        code=code,
                        details={"status": status, "url": sanitize_text(current)},
                    ) from exc
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    raise SkillToPluginError(
                        "The HTTPS download failed.",
                        code="network_failed",
                        details={"url": sanitize_text(current), "reason": sanitize_text(str(exc))},
                    ) from exc

                try:
                    status = getattr(response, "status", None)
                    if status is None:
                        status = response.getcode()
                    if status in _REDIRECT_STATUSES:
                        # Custom/fake openers may return redirects rather than
                        # raising HTTPError.  Apply exactly the same validation.
                        location = response.headers.get("Location")
                        if not location:
                            raise SkillToPluginError(
                                "The HTTPS source returned a redirect without a Location header.",
                                code="network_failed",
                                details={"status": status},
                            )
                        if len(redirects) >= self.max_redirects:
                            raise SkillToPluginError(
                                "The HTTPS source exceeded the redirect limit.",
                                code="network_failed",
                                details={"max_redirects": self.max_redirects},
                            )
                        target = urllib.parse.urljoin(current, location)
                        resolved = _validate_and_resolve_public_https_url(target, resolver=self.resolver)
                        current = resolved.url
                        redirects.append(current)
                        continue
                    if status < 200 or status >= 300:
                        raise SkillToPluginError(
                            "The HTTPS source returned a non-success status.",
                            code="network_failed",
                            details={"status": status, "url": sanitize_text(current)},
                        )

                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            declared = int(content_length)
                        except ValueError as exc:
                            raise SkillToPluginError(
                                "The HTTPS source returned an invalid Content-Length.",
                                code="network_failed",
                            ) from exc
                        if declared < 0 or declared > limit:
                            raise SkillToPluginError(
                                "The HTTPS response exceeds the configured download limit.",
                                code="security_rejected",
                                details={"declared_bytes": declared, "max_bytes": limit},
                            )

                    digest = hashlib.sha256()
                    size = 0
                    with temporary.open("wb") as handle:
                        while True:
                            chunk = response.read(min(1024 * 1024, limit - size + 1))
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > limit:
                                raise SkillToPluginError(
                                    "The HTTPS response exceeded the configured download limit while streaming.",
                                    code="security_rejected",
                                    details={"max_bytes": limit},
                                )
                            handle.write(chunk)
                            digest.update(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    content_type = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else response.headers.get("Content-Type")
                    temporary.replace(destination)
                    return HttpFetchResult(
                        url=current,
                        path=destination,
                        sha256=digest.hexdigest(),
                        size=size,
                        content_type=content_type,
                        redirects=tuple(redirects),
                    )
                finally:
                    if response is not None:
                        response.close()
        finally:
            temporary.unlink(missing_ok=True)


__all__ = ["HttpFetcher", "HttpFetchResult", "validate_public_https_url"]
