import asyncio
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import httpx

from .models import OAuthCredential, SinkReceipt


class SinkError(RuntimeError):
    pass


class CredentialSink(Protocol):
    async def store(self, credential: OAuthCredential) -> SinkReceipt: ...


def cpa_document(credential: OAuthCredential):
    # Free Grok chat requires cli-chat-proxy + grok-build OAuth (not api.x.ai billing path).
    base_url = getattr(credential, "base_url", None) or "https://cli-chat-proxy.grok.com/v1"
    doc = {
        "type": "xai",
        "access_token": credential.access_token,
        "refresh_token": credential.refresh_token,
        "id_token": credential.id_token,
        "token_type": credential.token_type,
        "expires_in": credential.expires_in,
        "expired": credential.expires_at,
        "last_refresh": credential.last_refresh,
        "sub": credential.subject,
        "email": getattr(credential, "email", None) or "",
        "base_url": base_url,
        "token_endpoint": credential.token_endpoint,
        "auth_kind": "oauth",
        "disabled": False,
    }
    redirect_uri = getattr(credential, "redirect_uri", None)
    if redirect_uri:
        doc["redirect_uri"] = redirect_uri
    headers = getattr(credential, "headers", None)
    if isinstance(headers, dict) and headers:
        doc["headers"] = headers
    return doc


def credential_filename(credential: OAuthCredential, name_secret: bytes):
    email = str(getattr(credential, "email", None) or "").strip().lower()
    if email and "@" in email:
        safe = "".join(ch if ch.isalnum() or ch in "._-+@" else "_" for ch in email)
        return f"xai-{safe}.json"
    subject = credential.subject or credential.refresh_token
    digest = hmac.new(name_secret, subject.encode(), hashlib.sha256).hexdigest()[:16]
    return f"xai-{digest}.json"


class CPAAuthFileSink:
    def __init__(self, base_url, management_secret, client: httpx.AsyncClient, name_secret=None):
        self.base_url = base_url.rstrip("/")
        self.management_secret = management_secret
        self.client = client
        self.name_secret = name_secret or management_secret.encode()

    async def store(self, credential: OAuthCredential):
        filename = credential_filename(credential, self.name_secret)
        document = cpa_document(credential)
        response = await self.client.post(
            f"{self.base_url}/v0/management/auth-files?{urlencode({'name': filename})}",
            headers={
                "Authorization": f"Bearer {self.management_secret}",
                "Content-Type": "application/json",
            },
            json=document,
            follow_redirects=False,
        )
        if response.status_code // 100 != 2:
            raise SinkError("CPA upload rejected")
        return SinkReceipt(filename.removesuffix(".json"))


class LocalAuthFileSink:
    """Atomically persist canonical CPA-compatible documents locally."""

    def __init__(self, directory, *, name_secret: bytes):
        self.directory = Path(directory).expanduser()
        self.name_secret = name_secret

    async def store(self, credential: OAuthCredential):
        return await asyncio.to_thread(self._store_sync, credential)

    def _store_sync(self, credential: OAuthCredential):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        filename = credential_filename(credential, self.name_secret)
        destination = self.directory / filename
        payload = json.dumps(cpa_document(credential), ensure_ascii=False, indent=2) + "\n"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}.", suffix=".tmp", dir=self.directory, text=True
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
            os.chmod(destination, 0o600)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return SinkReceipt(filename.removesuffix(".json"))


class MultiSink:
    """Store to multiple sinks; return last receipt (usually local or CPA)."""

    def __init__(self, sinks):
        self.sinks = list(sinks)
        if not self.sinks:
            raise ValueError("MultiSink requires at least one sink")

    async def store(self, credential: OAuthCredential) -> SinkReceipt:
        receipt = None
        errors = []
        for sink in self.sinks:
            try:
                receipt = await sink.store(credential)
            except Exception as exc:
                errors.append(f"{type(sink).__name__}:{exc}")
        if receipt is None:
            raise SinkError("; ".join(errors) or "all sinks failed")
        if errors:
            # Prefer success if any sink worked; surface soft warning via receipt still ok.
            pass
        return receipt

