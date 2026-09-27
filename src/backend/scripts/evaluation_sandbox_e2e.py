"""Explicit live probe: create two temporary evaluation boxes and always delete."""
import argparse
import asyncio
import json
import secrets
import shlex

from core.sandbox import evaluation_binding as bindings
from core.sandbox.errors import SandboxError
from core.sandbox.evaluation_provider import get_evaluation_provider
from core.sandbox.protocol import ProcessRequest
from core.services import evaluation_sandbox_service as service


async def denied(operation):
    try:
        await operation
    except SandboxError:
        return
    raise AssertionError("Operation unexpectedly authorized")


async def verify(image):
    owner = "eval-probe-" + secrets.token_hex(8)
    leases = []
    evidence = {"image": image, "checks": []}
    provider = get_evaluation_provider()
    try:
        for _ in range(2):
            leases.append(await service.create(owner, image=image, ttl_seconds=600))
        first, second = leases
        sid, lease = first["chat_id"], first["lease_id"]
        assert first["sandbox_id"] != second["sandbox_id"]
        evidence["sandbox_ids"] = [item["sandbox_id"] for item in leases]
        marker = secrets.token_hex(16).encode()
        await service.upload(lease, owner, "/attempt/workspace/seed", marker)
        assert await provider.get_file(sid, "/workspace/seed", user_id=owner) == marker
        await provider.put_file(sid, "/workspace/native-output", marker, user_id=owner)
        assert await service.download(lease, owner, "/attempt/workspace/native-output") == marker
        assert await provider.current_sandbox_id(sid) == first["sandbox_id"]
        evidence["checks"].append("native_and_control_same_instance")
        missing = await service.execute(
            second["lease_id"], owner, argv=["test", "!", "-e", "/workspace/seed"]
        )
        assert missing["exit_code"] == 0
        await denied(provider.get_file(sid, "/workspace/seed", user_id="foreign"))
        await denied(service.execute(lease, "foreign", argv=["true"]))
        evidence["checks"].extend(["attempt_isolation", "foreign_owner_denied"])
        result = await provider.run_to_completion(ProcessRequest(
            script_content="cat /workspace/seed", script_name="probe.sh",
            language="bash", session_id=sid, user_id=owner, timeout=20,
        ))
        assert result.exit_code == 0 and marker.decode() in result.stdout
        evidence["checks"].append("native_command_output")
        delayed = "sleep 3; echo contaminated > /workspace/late-write"
        result = await provider.run_to_completion(ProcessRequest(
            script_content="nohup bash -c " + shlex.quote(delayed) + " >/tmp/detached.log 2>&1 </dev/null &",
            script_name="detached.sh", language="bash", session_id=sid, user_id=owner, timeout=20,
        ))
        assert result.exit_code == 0
        await service.freeze(lease, owner)
        await denied(provider.put_file(sid, "/workspace/after-freeze", b"x", user_id=owner))
        await service.upload(lease, owner, "/attempt/workspace/gold-marker", b"hidden")
        await asyncio.sleep(4)
        result = await service.execute(lease, owner, argv=["test", "!", "-e", "/workspace/late-write"])
        assert result["exit_code"] == 0
        evidence["checks"].extend(["detached_writer_stopped", "frozen_agent_denied", "scorer_still_operates"])
    finally:
        failures = []
        for item in reversed(leases):
            try:
                await service.destroy(item["lease_id"], owner)
                assert (await bindings.get(item["chat_id"], allow_expired=True)).destroyed
                await service.destroy(item["lease_id"], owner)
                await denied(provider.get_file(item["chat_id"], "/workspace/seed", user_id=owner))
            except Exception as exc:
                failures.append(type(exc).__name__)
        if failures:
            raise RuntimeError("Probe sandbox cleanup failed: " + ", ".join(failures))
    evidence["checks"].append("destroy_idempotent_and_late_access_denied")
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Prepared ageval image with USER root")
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.image)), indent=2))
