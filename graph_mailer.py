# graph_mailer.py
"""Send e-mails (and create drafts) via Microsoft Graph.

Minimal synchronous mailer: no SharePoint, no upload, no attachments. It is the
trimmed twin of ``Trova_CodiceFornitura_PerBonus/services/graph/graph_client.py``
— same MSAL + ``requests`` shape, same atomic cache persistence, only ``sendMail``.

Identity — ABSOLUTE RULE
-----------------------
The whole work instance ecosystem shares ONE app registration and ONE set of MSAL
token caches living in the vault. This module therefore:

* never registers an app, never starts a device-code / interactive flow;
* only ever calls ``acquire_token_silent`` with the ``Mail.Send`` scope;
* fails cleanly (``send`` -> ``False`` + explicit log) when the silent refresh
  does not work, so refreshing the shared cache stays a deliberate act performed
  OUTSIDE this module (in the directory ``M365_TOKEN_CACHE_DIR`` points at,
  owned by the shared auth library).

Defaults (app registration, cache path, sender) are the installation's, read
from ``locale/defaults.json`` via :mod:`local_defaults`: the SERVICE account
(company resources), which is also the default sender. The PERSONAL cache
(``token_cache_user.json``) is for OneDrive files and must not be used here.

Docker mount — REQUIRED, NOT YET DONE
-------------------------------------
The Coordinator bot runs in a container, so the shared cache on the host
(``$M365_TOKEN_CACHE_DIR/token_cache.json``) is invisible
inside it until it is bind-mounted. Add to the ``bot`` service of
``docker-compose.yml``::

    volumes:
      # Shared M365 token cache (SERVICE account). Read-WRITE and mounted as a
      # DIRECTORY, not as a single file, on purpose:
      #  * :ro would break auth the first time Entra rotates the refresh token —
      #    MSAL would keep re-presenting a consumed RT and silently stop working;
      #  * a single-file bind mount would break _persist_cache, which writes a
      #    sibling tmp file and os.replace()s it over the target (atomic swap):
      #    that rename fails with EBUSY on a bind-mounted file, and the tmp file
      #    needs write permission on the directory anyway.
      - ${M365_TOKEN_CACHE_DIR:-./data/m365}:/m365
    # NOT under /secrets: that path is already a read-only mount, and Docker
    # cannot create a writable mountpoint inside one — the container would
    # refuse to start with "read-only file system".
      # Module code, like the other modules
      - ./graph_mailer.py:/app/graph_mailer.py:ro
    environment:
      MICROSOFT_TOKEN_CACHE_FILE: /m365/token_cache.json

Permissions line up as is: the image user ``app`` is uid 1000 (``useradd -m app``
on a fresh image) and the host vault files belong to ``dev`` = uid 1000.

Runtime dependency: ``msal`` (which pulls ``requests``) is NOT in
``requirements.txt`` yet — the container will log an error and refuse to send
until it is added.
"""

from __future__ import annotations

import importlib
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

import local_defaults
from atomic_io import replace_with_retry

_APP_AUTH_KEY = "GRAPH_APP_AUTH"


def _load_app_auth(spec: str):
    """Resolve the app-only auth factory named by the installation.

    ``spec`` is ``"module:callable"`` (``GRAPH_APP_AUTH``, env or locale/). The
    callable takes ``tenant_id``, ``client_id``, ``client_secret`` and ``log``
    keywords and returns an object with ``token() -> str | None`` and an
    ``ultimo_errore`` attribute. The engine names no library: which one serves
    app-only is the installation's choice, and it may not be installable at all
    from a public clone. Unset or not importable -> None: the delegated path
    (plain msal + shared cache) still works, app-only logs and stays off.
    """
    module_name, _, attr = spec.strip().partition(":")
    if not module_name or not attr:
        return None
    try:
        return getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError):
        logging.getLogger(__name__).warning(
            "%s=%s not importable: app-only MS Graph is off.", _APP_AUTH_KEY, spec
        )
        return None


app_auth = _load_app_auth(local_defaults.get(_APP_AUTH_KEY, ""))

_GRAPH = "https://graph.microsoft.com/v1.0"
# Only what this module does. Asking for more would make the silent refresh fail
# on a cache consented for a narrower set.
# Gli scope, per operazione. `Mail.Send` basta per inviare; creare una BOZZA e'
# un POST su /messages e vuole scrittura sulla cassetta — non `Mail.ReadWrite`,
# che sull'app condivisa di work instance e' stato rifiutato, ma
# `Mail.ReadWrite.Shared` («la propria posta e quella condivisa» nella
# documentazione Graph), che sul login di agosto c'era gia'.
_SCOPE_INVIO = ["Mail.Send"]
_SCOPE_BOZZA = ["Mail.ReadWrite.Shared"]

# Cosa si CHIEDE: l'unione. MSAL e' tutto-o-niente sull'insieme richiesto, e
# chiedere un sottoinsieme risalvando la cache la RESTRINGE — e' cosi' che il
# 10/09 sono spariti gli scope Chat.* e sono morti in blocco sei consumer.
# Questo modulo parla con msal direttamente, quindi l'unione se la deve fare da
# se': non e' protetto da GraphAuth.
_SCOPES = _SCOPE_INVIO + _SCOPE_BOZZA

_TIMEOUT = 30  # seconds, per HTTP request
_MAX_ATTEMPTS = 2  # one send + at most one retry (429/503 only)
_MAX_RETRY_WAIT = 60.0  # cap on the honoured Retry-After

# The installation's identity (app registration, cache, sender) comes from
# locale/defaults.json — see local_defaults. The engine ships none: on a fresh
# install these are blank and the mailer reports itself disabled.
DEFAULT_CLIENT_ID = local_defaults.get("MICROSOFT_CLIENT_ID", env={})
DEFAULT_TENANT_ID = local_defaults.get("MICROSOFT_TENANT_ID", env={})
DEFAULT_TOKEN_CACHE_FILE = local_defaults.get("MICROSOFT_TOKEN_CACHE_FILE", env={})
DEFAULT_SENDER = local_defaults.get("EMAIL_SENDER", env={})

# Injectable in tests so a retry does not really sleep.
_sleep = time.sleep


@dataclass(frozen=True)
class MailerConfig:
    """Everything the mailer needs; defaults are the shared ecosystem identity."""

    client_id: str = DEFAULT_CLIENT_ID
    tenant_id: str = DEFAULT_TENANT_ID
    token_cache_file: str = DEFAULT_TOKEN_CACHE_FILE
    sender: str = DEFAULT_SENDER
    # Valorizzato -> app-only: il token e' dell'applicazione e si rinnova da
    # solo, quindi la cache condivisa non serve piu'. Vuoto -> il percorso
    # delegato qui sotto, che dipende da quella cache.
    client_secret: str = ""

    @property
    def app_only(self) -> bool:
        return bool(self.client_secret)

    @property
    def enabled(self) -> bool:
        """False when any piece is blank — the caller can skip the send entirely.

        In app-only the shared cache file is irrelevant: there is no cache.
        """
        if not (self.client_id and self.tenant_id and self.sender):
            return False
        return bool(self.client_secret or self.token_cache_file)


def _str(env: Mapping[str, str], key: str, default: str) -> str:
    """Env override, falling back to the default when unset OR blank.

    A blank var (``EMAIL_SENDER=``) is a deployment slip, not a request to send
    from nobody: treat it as absent instead of building a broken config.
    """
    return (env.get(key) or "").strip() or default


def load_config(env: Mapping[str, str] | None = None) -> MailerConfig:
    """Build the mailer config from env (``os.environ`` unless one is passed)."""
    env = os.environ if env is None else env
    return MailerConfig(
        client_id=_str(env, "MICROSOFT_CLIENT_ID", DEFAULT_CLIENT_ID),
        tenant_id=_str(env, "MICROSOFT_TENANT_ID", DEFAULT_TENANT_ID),
        token_cache_file=_str(
            env, "MICROSOFT_TOKEN_CACHE_FILE", DEFAULT_TOKEN_CACHE_FILE
        ),
        sender=_str(env, "EMAIL_SENDER", DEFAULT_SENDER),
        client_secret=(env.get("MICROSOFT_CLIENT_SECRET") or "").strip(),
    )


class GraphMailer:
    """Silent-only Microsoft Graph mailer. Never raises out of :meth:`send`."""

    def __init__(
        self, config: MailerConfig | None = None, *, logger: Any = None
    ) -> None:
        self._cfg = config or load_config()
        self._log: logging.Logger = logger or logging.getLogger(__name__)
        # Lazy state: msal app + its token cache, built on first use.
        self._app: Any = None
        self._cache: Any = None

    # ------------------------------------------------------------------ auth
    def _ensure_app(self) -> bool:
        """Build the MSAL public client + load the shared cache. False if impossible."""
        if self._app is not None:
            return True

        path = self._cfg.token_cache_file
        if not os.path.exists(path):
            # The expected failure until the compose mount above exists.
            self._log.error(
                "MSAL token cache not found at %s: e-mail sending is off. Inside Docker "
                "the shared vault cache must be bind-mounted read-write (see the "
                "graph_mailer docstring). Do NOT start a device-code login.",
                path,
            )
            return False

        try:
            import msal  # local import: heavy dependency, only needed here
        except ImportError:
            self._log.error(
                "msal is not installed: e-mail sending is off. Add `msal` to "
                "requirements.txt and rebuild the image."
            )
            return False

        cache = msal.SerializableTokenCache()
        try:
            with open(path, encoding="utf-8") as f:
                cache.deserialize(f.read())
        except (OSError, ValueError) as exc:
            self._log.error("Unreadable MSAL token cache %s: %r", path, exc)
            return False

        self._cache = cache
        self._app = msal.PublicClientApplication(
            self._cfg.client_id,
            authority=f"https://login.microsoftonline.com/{self._cfg.tenant_id}",
            token_cache=cache,
            # msal drives `requests` internally and defaults to timeout=None: bound it.
            timeout=_TIMEOUT,
        )
        return True

    def _persist_cache(self) -> None:
        """Rewrite the token cache ATOMICALLY, and only when its state changed.

        The file is SHARED with the rest of the ecosystem: an in-place ``open('w')``
        would truncate it, and a crash halfway (or a concurrent reader) would break
        authentication for every project. Write a tmp file in the same directory and
        ``os.replace`` it — a rename on the same filesystem, atomic for readers.
        Never raises: a failed persist costs a refresh, not the e-mail.
        """
        if self._cache is None or not getattr(self._cache, "has_state_changed", False):
            return
        path = self._cfg.token_cache_file
        dir_ = os.path.dirname(path) or "."
        tmp = ""
        try:
            os.makedirs(dir_, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".token_cache_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self._cache.serialize())
                f.flush()
                os.fsync(f.fileno())
            replace_with_retry(tmp, path)  # atomic: no truncated file for readers
        except OSError as exc:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self._log.error("Could not persist the MSAL token cache %s: %r", path, exc)
            return
        self._log.debug("MSAL token cache refreshed atomically: %s", path)

    @staticmethod
    def _pick_account(
        accounts: list[dict[str, Any]], sender: str
    ) -> dict[str, Any] | None:
        """The sender's account in the shared cache, else the first one.

        The cache may hold several identities; matching the configured sender keeps
        the SERVICE account from silently borrowing a personal one.
        """
        wanted = (sender or "").strip().casefold()
        for account in accounts:
            if (account.get("username") or "").strip().casefold() == wanted:
                return account
        return accounts[0] if accounts else None

    def _acquire_token(self, *, minimi: list[str] | None = None) -> str | None:
        """Access token. App-only when a secret is configured, silent refresh otherwise.

        ``minimi`` sono gli scope senza i quali l'operazione in corso non puo'
        procedere. Si chiede sempre l'unione (:data:`_SCOPES`); se
        l'allargamento fallisce — tipicamente perche' un permesso e' stato
        revocato altrove — si ripiega su quelli, **senza risalvare la cache**.

        Le due meta' di questa regola servono a cose opposte, e servono
        entrambe. Chiedere l'unione evita di restringere un refresh token
        condiviso con tutto l'ecosistema. Ripiegare evita che l'aggiunta di
        `Mail.ReadWrite.Shared` per le bozze (#24) spenga l'invio, che
        funziona e non c'entra niente: una funzione nuova non deve poter
        rompere una vecchia.
        """
        if self._cfg.app_only:
            # The factory comes from the installation (GRAPH_APP_AUTH). No
            # cache, no account to pick — the token is the application's own.
            if app_auth is None:
                self._log.error(
                    "App-only MS Graph needs an auth factory (%s), none "
                    "configured or importable: e-mail sending is off.",
                    _APP_AUTH_KEY,
                )
                return None
            auth = app_auth(
                tenant_id=self._cfg.tenant_id,
                client_id=self._cfg.client_id,
                client_secret=self._cfg.client_secret,
                log=self._log.info,
            )
            token = auth.token()
            if token is None:
                self._log.error(
                    "App-only MS Graph token unavailable (%s): e-mail sending is off.",
                    auth.ultimo_errore,
                )
            return token

        if not self._ensure_app():
            return None

        accounts = self._app.get_accounts() or []
        account = self._pick_account(accounts, self._cfg.sender)
        if account is None:
            self._log.error(
                "No account in the shared MSAL cache %s: cannot send as %s. Refresh the "
                "shared cache where it lives (M365_TOKEN_CACHE_DIR) — never from here.",
                self._cfg.token_cache_file,
                self._cfg.sender,
            )
            return None

        result = self._app.acquire_token_silent(_SCOPES, account=account)
        # The silent call may have rotated the refresh token even when it then
        # failed: persist first, so a partial success is not thrown away.
        self._persist_cache()

        minimi = minimi or _SCOPE_INVIO
        if (not result or "access_token" not in result) and set(minimi) != set(_SCOPES):
            self._log.warning(
                "Rinnovo silenzioso con l'insieme completo %s fallito (%s): "
                "ritento con i soli %s. La cache NON viene risalvata, o un "
                "insieme piu' povero diventerebbe quello di tutti.",
                _SCOPES,
                (result or {}).get("error"),
                minimi,
            )
            result = self._app.acquire_token_silent(minimi, account=account)
            # Deliberatamente nessun _persist_cache() qui: vedi sopra.

        if not result or "access_token" not in result:
            err = (result or {}).get("error")
            desc = (result or {}).get("error_description")
            self._log.error(
                "Silent MS Graph refresh failed for %s (%s: %s). The shared cache is "
                "expired or revoked: refresh it where it lives (M365_TOKEN_CACHE_DIR). This "
                "module never opens an interactive login.",
                self._cfg.sender,
                err,
                desc,
            )
            return None
        return result["access_token"]

    # --------------------------------------------------------------- sending
    @staticmethod
    def _retry_after(resp: requests.Response, attempt: int) -> float:
        """Seconds to wait: server's Retry-After when sane, else capped backoff."""
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return min(max(float(header), 0.0), _MAX_RETRY_WAIT)
            except ValueError:
                pass
        return min(float(2**attempt), _MAX_RETRY_WAIT)

    def send(
        self,
        *,
        to: str,
        subject: str,
        body_html: str,
        cc: list[str] | None = None,
    ) -> bool:
        """Send an HTML e-mail from the configured sender. True only on HTTP 202.

        NEVER raises: the caller is a Telegram callback that must survive an
        unreachable Graph, an expired cache, a corrupt cache file or a malformed
        recipient. Anything unforeseen is logged with its traceback and reported
        as a plain False — a failed notification must not kill the bot.
        """
        try:
            return self._send(to=to, subject=subject, body_html=body_html, cc=cc)
        except Exception:  # noqa: BLE001 - deliberate last-resort guard, see docstring
            self._log.exception("Unexpected failure while sending the e-mail to %r", to)
            return False

    def create_draft(
        self,
        *,
        to: str,
        subject: str,
        body_html: str,
        cc: list[str] | None = None,
    ) -> bool:
        """Crea una BOZZA nella cassetta del mittente. True solo su 201.

        Non invia, e non deve poterlo fare nemmeno per errore: e' il senso
        dell'azione `email_draft` del maggiordomo (#20), dove una preparazione
        che diventasse un atto irreversibile verso l'esterno sarebbe il guasto
        peggiore possibile. Per questo non c'e' nessun ripiego su :meth:`send`
        — un fallimento resta un fallimento.

        Come :meth:`send`, non solleva mai: il chiamante e' una callback
        Telegram che deve sopravvivere a un Graph irraggiungibile, a una cache
        scaduta o corrotta e a un destinatario malformato.
        """
        try:
            return self._create_draft(to=to, subject=subject, body_html=body_html, cc=cc)
        except Exception:  # noqa: BLE001 - stessa guardia di send(), stesso motivo
            self._log.exception("Fallimento imprevisto creando la bozza per %r", to)
            return False

    def _create_draft(
        self, *, to: str, subject: str, body_html: str, cc: list[str] | None
    ) -> bool:
        """Creazione vera. I chiamanti passano da :meth:`create_draft`."""
        recipient = (to or "").strip()
        if not recipient:
            self._log.error(
                "create_draft() chiamata senza destinatario (oggetto: %r)", subject
            )
            return False

        token = self._acquire_token(minimi=_SCOPE_BOZZA)
        if token is None:
            return False  # gia' loggato, con il rimedio

        # Il messaggio, nudo: senza `sendMail` attorno e senza
        # `saveToSentItems`, perche' non parte niente.
        payload: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        }
        cc_list = [a.strip() for a in (cc or []) if a and a.strip()]
        if cc_list:
            payload["ccRecipients"] = [
                {"emailAddress": {"address": a}} for a in cc_list
            ]

        url = f"{_GRAPH}/users/{quote(self._cfg.sender, safe='@')}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = requests.post(
                    url, headers=headers, json=payload, timeout=_TIMEOUT
                )
            except requests.RequestException as exc:
                # A differenza di sendMail questa POST e' innocua se ripetuta
                # (al peggio due bozze), ma una rete caduta a meta' non dice se
                # la prima e' arrivata: meglio una bozza mancante che due.
                self._log.error(
                    "create_draft per %s fallita (rete): %r", recipient, exc
                )
                return False

            if resp.status_code == 201:
                self._log.info(
                    "Bozza creata per %s (oggetto: %r)", recipient, subject
                )
                return True

            if resp.status_code in (429, 503) and attempt < _MAX_ATTEMPTS - 1:
                wait = self._retry_after(resp, attempt)
                self._log.warning(
                    "create_draft per %s -> %s, un solo nuovo tentativo fra %.0fs",
                    recipient,
                    resp.status_code,
                    wait,
                )
                _sleep(wait)
                continue

            self._log.error(
                "create_draft per %s fallita: %s %s",
                recipient,
                resp.status_code,
                resp.text[:500],
            )
            return False

        return False

    def _send(
        self, *, to: str, subject: str, body_html: str, cc: list[str] | None
    ) -> bool:
        """Actual send. Callers go through :meth:`send`, which swallows surprises."""
        recipient = (to or "").strip()
        if not recipient:
            self._log.error("send() called without a recipient (subject: %r)", subject)
            return False

        token = self._acquire_token(minimi=_SCOPE_INVIO)
        if token is None:
            return False  # already logged, with the remediation

        payload: dict[str, Any] = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            },
            "saveToSentItems": True,
        }
        cc_list = [a.strip() for a in (cc or []) if a and a.strip()]
        if cc_list:
            payload["message"]["ccRecipients"] = [
                {"emailAddress": {"address": a}} for a in cc_list
            ]

        # `@` stays literal: it is legal in a path segment and it is the form
        # Graph documents, so the URL reads like the one in the docs.
        url = f"{_GRAPH}/users/{quote(self._cfg.sender, safe='@')}/sendMail"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = requests.post(
                    url, headers=headers, json=payload, timeout=_TIMEOUT
                )
            except requests.RequestException as exc:
                # sendMail is not idempotent and the request may already have
                # reached Graph: retrying could send the mail twice.
                self._log.error("sendMail to %s failed (network): %r", recipient, exc)
                return False

            if resp.status_code == 202:
                self._log.info("E-mail sent to %s (subject: %r)", recipient, subject)
                return True

            # Throttling / transient unavailability: worth exactly one retry.
            if resp.status_code in (429, 503) and attempt < _MAX_ATTEMPTS - 1:
                wait = self._retry_after(resp, attempt)
                self._log.warning(
                    "sendMail to %s -> %s, retrying once in %.0fs",
                    recipient,
                    resp.status_code,
                    wait,
                )
                _sleep(wait)
                continue

            # Any other 4xx is a permanent request problem: retrying repeats it.
            self._log.error(
                "sendMail to %s failed: %s %s",
                recipient,
                resp.status_code,
                resp.text[:500],
            )
            return False

        return False

