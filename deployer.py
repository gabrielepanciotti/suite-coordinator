# deployer.py
"""Sidecar deployer: SSE-listen on the bus as deployer-<device>, execute typed
infra commands (pull/recreate-worker/set-env/deploy) from an allowlisted sender,
post the outcome back. A SEPARATE container from coordinator-worker, so recreating
the worker never kills this process. See docs/superpowers/specs/2026-06-12-sidecar-deployer-design.md.
"""

from __future__ import annotations

import asyncio
import logging

import deployer_config
import deployer_ops
import hub_client

logger = logging.getLogger("coordinator.deployer")

# Idempotency: message ids already executed. Prevents a redelivered SSE event
# (after a reconnect) from double-executing. Bounded by process lifetime; a
# restart resumes from the live cursor so the bus never replays old events.
_processed: set[int] = set()


async def _execute(
    cfg: deployer_config.DeployerConfig, verb: str, args: dict
) -> deployer_ops.OpResult:
    if verb == "pull":
        return await deployer_ops.op_pull(cfg)
    if verb == "recreate-worker":
        return await deployer_ops.op_recreate_worker(cfg)
    if verb == "set-env":
        return await deployer_ops.op_set_env(
            cfg, args.get("key", ""), args.get("value", "")
        )
    if verb == "deploy":
        return await deployer_ops.op_deploy(cfg)
    if verb == "verify":
        return await deployer_ops.op_verify(cfg)
    if verb == "rollback":
        return await deployer_ops.op_rollback(cfg, args.get("to_sha", ""))
    return deployer_ops.OpResult(False, f"unknown verb: {verb!r}")


async def _read_command(client, conv_id: int, message_id: int) -> dict | None:
    """Fetch the conv and return the full message `context` (deployer command +
    pipeline correlation, if any). The SSE new_message event omits context, so
    we read it authoritatively here."""
    conv = await client.get_conversation(conv_id)
    for m in conv.get("messages", []):
        if int(m.get("id", 0)) == int(message_id):
            return m.get("context") or None
    return None


async def handle_event(
    cfg: deployer_config.DeployerConfig, client, ev_type: str, data: dict
) -> None:
    if ev_type != "new_message":
        return
    sender = data.get("from_agent")
    conv_id = data.get("conversation_id")
    message_id = data.get("message_id")
    if conv_id is None or message_id is None:
        return
    # Cheap sender gate FIRST: an unlisted sender never triggers a fetch.
    if sender not in cfg.allowed_senders:
        logger.info("deployer: ignoring message from %s (not allowed)", sender)
        return
    if int(message_id) in _processed:
        logger.info("deployer: message %s already processed, skipping", message_id)
        return
    context = await _read_command(client, conv_id, message_id)
    cmd = (context or {}).get("deployer")
    corr = (context or {}).get("pipeline")
    if not cmd:
        return  # not a deployer command
    # Mark processed only after we have the command and before executing: the
    # _listen loop is single-consumer (one handle_event awaited at a time, with
    # reconnect only after the stream ends), so there is no concurrent entry to
    # race on. Adding the id BEFORE the fetch would instead lose the command
    # silently if the fetch raised (the id would look "done" and never retry) —
    # exactly the silent-loss failure mode the wake-reliability work forbids.
    _processed.add(int(message_id))
    verb = cmd.get("verb", "")
    args = cmd.get("args") or {}
    # Never log a set-env value at INFO: it may carry config a human would not
    # want in the log stream (secrets are already blocked upstream by the
    # whitelist, but mask defensively anyway).
    log_args = args
    if verb == "set-env" and isinstance(args, dict) and "value" in args:
        log_args = {**args, "value": "***"}
    logger.info("deployer: executing %s args=%s (from %s)", verb, log_args, sender)
    result = await _execute(cfg, verb, args)
    text = result.format()
    verdict_ctx = None
    if isinstance(corr, dict) and "id" in corr and "stage" in corr:
        # Guard the coercion symmetrically with parse_verdict: a malformed
        # correlation id must not raise after the verb already ran (that would
        # strand the pipeline stage with the message already deduped). Degrade
        # to a text-only reply instead.
        try:
            vid = int(corr["id"])
        except (TypeError, ValueError):
            logger.warning("deployer: malformed pipeline correlation id=%r, "
                           "replying text-only", corr.get("id"))
        else:
            sha = await deployer_ops.head_sha(cfg)
            verdict_ctx = {"pipeline_verdict": {
                "id": vid, "stage": str(corr["stage"]),
                "ok": result.ok, "summary": result.summary, "sha": sha or None,
            }}
    try:
        if verdict_ctx is not None:
            await client.send_message(conv_id, text, context=verdict_ctx)
        else:
            await client.send_message(conv_id, text)
    except Exception:
        logger.exception("deployer: failed to post outcome for #%s", conv_id)


async def _listen(cfg: deployer_config.DeployerConfig) -> None:
    """Reconnecting SSE loop. Tracks last_event_id so a dropped connection
    resumes; idempotency is by message-id, not cursor."""
    client = hub_client.HubClient(
        cfg.hub_url, cfg.api_key, cfg.agent_id, cfg.agent_key
    )
    last_id: int | None = None
    # Say who holds this identity, like hub._listen does (#37): without it the
    # fleet shows the deployer as `unowned`, held by a bare container id.
    await hub_client.dichiara_proprietario(
        cfg.hub_url, cfg.api_key, cfg.agent_id, cfg.agent_key
    )
    while True:
        try:
            async for ev_type, payload, ev_id in hub_client.stream_events(
                cfg.hub_url, cfg.api_key, cfg.agent_id, last_event_id=last_id,
                agent_key=cfg.agent_key,
            ):
                await handle_event(cfg, client, ev_type, payload)
                if ev_id:
                    last_id = ev_id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("deployer SSE dropped (%s), reconnecting in 5s", exc)
            await asyncio.sleep(5)


async def _main() -> None:
    cfg = deployer_config.load_deployer_config()
    if not cfg.enabled:
        raise SystemExit(
            "deployer config incomplete (need HUB_URL, api key, "
            "DEPLOYER_AGENT_ID, DEPLOYER_ALLOWED_SENDERS)"
        )
    logger.info(
        "deployer up: agent=%s senders=%s worker=%s",
        cfg.agent_id, sorted(cfg.allowed_senders), cfg.worker_service,
    )
    await _listen(cfg)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
