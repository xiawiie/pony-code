"""Release-only identity owner for Docker network control fixtures."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets

from pico import security as securitylib


FORMAT_VERSION = 1
MAX_OWNER_BYTES = 256 * 1024
MAX_DOCKER_RESPONSE_BYTES = 4 * 1024 * 1024

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_OWNER_FIELDS = {
    "record_type",
    "format_version",
    "phase",
    "owner_digest",
    "client_identity_digest",
    "release_binding_digest",
    "nonce",
    "labels",
    "image_reference",
    "image_id",
    "peer_argv",
    "network_name",
    "network_id",
    "peer_name",
    "peer_id",
}
_RESULT_FIELDS = {
    "record_type",
    "format_version",
    "status",
    "reason_code",
    "owner_digest",
    "topology_digest",
    "endpoint_digest",
    "facts",
    "cleanup",
    "evidence_digest",
}
_FACT_FIELDS = {
    "control_peer_dns_reachable",
    "control_peer_tcp_reachable",
    "control_peer_udp_reachable",
    "control_gateway_reachable",
    "control_host_reachable",
    "production_peer_dns_denied",
    "production_peer_tcp_denied",
    "production_peer_udp_denied",
    "production_gateway_denied",
    "production_host_denied",
    "host_to_guest_denied",
}
_CLEANUP_FIELDS = {
    "status",
    "inventory_complete",
    "containers_remaining",
    "networks_remaining",
}


class NetworkControlError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _digest(domain, value):
    return "sha256:" + hashlib.sha256(domain + _canonical_json(value)).hexdigest()


def _decode_json(raw):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=object_pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("invalid JSON constant")
        ),
    )


def _valid_digest(value):
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _valid_id(value):
    return type(value) is str and _HEX64_RE.fullmatch(value) is not None


def _valid_name(value):
    return type(value) is str and _SAFE_NAME_RE.fullmatch(value) is not None


def _parse_id(result):
    if not _clean_result(result):
        return ""
    try:
        value = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    return value if _valid_id(value) else ""


def _clean_result(result):
    return bool(
        result is not None
        and result.timed_out is False
        and result.exit_code == 0
        and result.stdout_truncated is False
        and result.stderr_truncated is False
        and result.stdout_bytes == len(result.stdout)
        and result.stderr_bytes == len(result.stderr)
    )


def _inspect_json(client, kind, object_id):
    result = client.command(
        [kind, "inspect", object_id, "--format", "{{json .}}"],
        timeout=30,
        max_bytes=MAX_DOCKER_RESPONSE_BYTES,
    )
    if not _clean_result(result) or result.stderr:
        raise NetworkControlError("network_control_inventory_failed")
    try:
        payload = _decode_json(result.stdout)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise NetworkControlError("network_control_inventory_failed") from exc
    if not isinstance(payload, dict):
        raise NetworkControlError("network_control_inventory_failed")
    return payload


def _list_ids(client, kind, labels, *, object_id=None):
    args = [kind, "ls"]
    if kind == "container":
        args.extend(("--all", "--quiet", "--no-trunc"))
    else:
        args.extend(("--quiet", "--no-trunc"))
    if object_id:
        args.extend(("--filter", "id=" + object_id))
    for key, value in sorted(labels.items()):
        args.extend(("--filter", f"label={key}={value}"))
    result = client.command(args, timeout=30, max_bytes=MAX_DOCKER_RESPONSE_BYTES)
    if not _clean_result(result) or result.stderr:
        raise NetworkControlError("network_control_inventory_failed")
    try:
        values = tuple(
            line for line in result.stdout.decode("ascii").splitlines() if line
        )
    except UnicodeDecodeError as exc:
        raise NetworkControlError("network_control_inventory_failed") from exc
    if any(not _valid_id(value) for value in values) or len(set(values)) != len(values):
        raise NetworkControlError("network_control_inventory_failed")
    return values


@dataclass(frozen=True)
class NetworkControlTopology:
    topology_digest: str
    peer_alias: str
    peer_ipv4: str
    gateway_ipv4: str


@dataclass(frozen=True)
class NetworkControlEndpoints:
    topology_digest: str
    peer_alias: str
    peer_ipv4: str
    peer_tcp_port: int
    peer_udp_port: int
    gateway_ipv4: str
    gateway_tcp_port: int
    host_address: str
    host_tcp_port: int
    guest_tcp_port: int

    @property
    def endpoint_digest(self):
        return _digest(b"PICO_NETWORK_CONTROL_ENDPOINTS_V1\0", asdict(self))


@dataclass(frozen=True)
class NetworkControlPositiveFacts:
    endpoints: NetworkControlEndpoints
    challenge_nonce: str
    peer_dns_reachable: bool
    peer_tcp_reachable: bool
    peer_udp_reachable: bool
    gateway_reachable: bool
    host_reachable: bool


@dataclass(frozen=True)
class NetworkControlNegativeFacts:
    endpoint_digest: str
    peer_dns_denied: bool
    peer_tcp_denied: bool
    peer_udp_denied: bool
    gateway_denied: bool
    host_denied: bool
    host_to_guest_denied: bool


@dataclass(frozen=True)
class NetworkControlCleanup:
    status: str
    inventory_complete: bool
    containers_remaining: int
    networks_remaining: int


def _strict_endpoint(value, topology):
    if (
        not isinstance(value, NetworkControlEndpoints)
        or value.topology_digest != topology.topology_digest
        or value.peer_alias != topology.peer_alias
        or value.peer_ipv4 != topology.peer_ipv4
        or value.gateway_ipv4 != topology.gateway_ipv4
        or any(
            type(getattr(value, name)) is not int
            or not 0 < getattr(value, name) <= 65535
            for name in (
                "peer_tcp_port",
                "peer_udp_port",
                "gateway_tcp_port",
                "host_tcp_port",
                "guest_tcp_port",
            )
        )
        or not isinstance(value.host_address, str)
        or not value.host_address
        or any(ord(character) < 32 for character in value.host_address)
    ):
        raise NetworkControlError("network_control_probe_invalid")
    return value


def _strict_facts(positive, negative, topology, nonce):
    if (
        not isinstance(positive, NetworkControlPositiveFacts)
        or positive.challenge_nonce != nonce
        or any(
            type(getattr(positive, name)) is not bool
            for name in (
                "peer_dns_reachable",
                "peer_tcp_reachable",
                "peer_udp_reachable",
                "gateway_reachable",
                "host_reachable",
            )
        )
    ):
        raise NetworkControlError("network_control_probe_invalid")
    endpoints = _strict_endpoint(positive.endpoints, topology)
    if (
        not isinstance(negative, NetworkControlNegativeFacts)
        or negative.endpoint_digest != endpoints.endpoint_digest
        or any(
            type(getattr(negative, name)) is not bool
            for name in (
                "peer_dns_denied",
                "peer_tcp_denied",
                "peer_udp_denied",
                "gateway_denied",
                "host_denied",
                "host_to_guest_denied",
            )
        )
    ):
        raise NetworkControlError("network_control_probe_invalid")
    return endpoints, {
        "control_peer_dns_reachable": positive.peer_dns_reachable,
        "control_peer_tcp_reachable": positive.peer_tcp_reachable,
        "control_peer_udp_reachable": positive.peer_udp_reachable,
        "control_gateway_reachable": positive.gateway_reachable,
        "control_host_reachable": positive.host_reachable,
        "production_peer_dns_denied": negative.peer_dns_denied,
        "production_peer_tcp_denied": negative.peer_tcp_denied,
        "production_peer_udp_denied": negative.peer_udp_denied,
        "production_gateway_denied": negative.gateway_denied,
        "production_host_denied": negative.host_denied,
        "host_to_guest_denied": negative.host_to_guest_denied,
    }


def _cleanup_payload(cleanup):
    return {
        "status": cleanup.status,
        "inventory_complete": cleanup.inventory_complete,
        "containers_remaining": cleanup.containers_remaining,
        "networks_remaining": cleanup.networks_remaining,
    }


def _result(owner_digest, topology_digest, endpoint_digest, facts, cleanup):
    passed = bool(all(facts.values()) and cleanup.status == "completed")
    if cleanup.status != "completed":
        reason = "network_control_cleanup_failed"
    elif not all(facts.values()):
        reason = "network_control_behavior_failed"
    else:
        reason = "verified"
    payload = {
        "record_type": "docker_sandbox_network_control_result",
        "format_version": FORMAT_VERSION,
        "status": "passed" if passed else "failed",
        "reason_code": reason,
        "owner_digest": owner_digest,
        "topology_digest": topology_digest,
        "endpoint_digest": endpoint_digest,
        "facts": dict(facts),
        "cleanup": _cleanup_payload(cleanup),
    }
    payload["evidence_digest"] = _digest(
        b"PICO_NETWORK_CONTROL_RESULT_V1\0", payload
    )
    return validate_network_control_result(payload)


def validate_network_control_result(value):
    if not isinstance(value, dict) or set(value) != _RESULT_FIELDS:
        raise ValueError("invalid network control result")
    facts = value["facts"]
    cleanup = value["cleanup"]
    unsigned = {key: item for key, item in value.items() if key != "evidence_digest"}
    if (
        value["record_type"] != "docker_sandbox_network_control_result"
        or value["format_version"] != FORMAT_VERSION
        or value["status"] not in {"passed", "failed"}
        or value["reason_code"]
        not in {
            "verified",
            "network_control_behavior_failed",
            "network_control_cleanup_failed",
        }
        or any(
            not _valid_digest(value[name])
            for name in (
                "owner_digest",
                "topology_digest",
                "endpoint_digest",
                "evidence_digest",
            )
        )
        or not isinstance(facts, dict)
        or set(facts) != _FACT_FIELDS
        or any(type(item) is not bool for item in facts.values())
        or not isinstance(cleanup, dict)
        or set(cleanup) != _CLEANUP_FIELDS
        or cleanup["status"] not in {"completed", "failed"}
        or type(cleanup["inventory_complete"]) is not bool
        or any(
            type(cleanup[name]) is not int or cleanup[name] < 0
            for name in ("containers_remaining", "networks_remaining")
        )
        or value["evidence_digest"]
        != _digest(b"PICO_NETWORK_CONTROL_RESULT_V1\0", unsigned)
    ):
        raise ValueError("invalid network control result")
    passed = all(facts.values()) and cleanup == {
        "status": "completed",
        "inventory_complete": True,
        "containers_remaining": 0,
        "networks_remaining": 0,
    }
    expected_reason = (
        "network_control_cleanup_failed"
        if cleanup["status"] != "completed"
        else "verified"
        if all(facts.values())
        else "network_control_behavior_failed"
    )
    if value["status"] != ("passed" if passed else "failed") or value[
        "reason_code"
    ] != expected_reason:
        raise ValueError("invalid network control result")
    return value


class NetworkControl:
    """Own one worker's release-only control network and peer container."""

    def __init__(self, client, state_root, owner):
        self.client = client
        self.state_root = Path(state_root)
        self.owner_path = self.state_root / "owner.json"
        self._root_identity = securitylib.private_directory_identity(self.state_root)
        self.owner = owner

    @classmethod
    def open(
        cls,
        client,
        state_root,
        *,
        release_binding,
        image_reference,
        image_id,
        peer_argv,
        nonce=None,
    ):
        state_root = securitylib.ensure_private_dir(state_root)
        path = state_root / "owner.json"
        binding_digest = _digest(
            b"PICO_NETWORK_CONTROL_RELEASE_BINDING_V1\0", release_binding
        )
        client_digest = client.identity_digest()
        if (
            not _valid_digest(client_digest)
            or not _valid_digest(image_reference)
            or not _valid_digest(image_id)
            or not isinstance(peer_argv, (tuple, list))
            or not peer_argv
            or any(
                type(item) is not str or not item or "\x00" in item
                for item in peer_argv
            )
        ):
            raise NetworkControlError("network_control_input_invalid")
        if path.exists():
            owner = cls._read_owner(state_root, path)
            immutable = {
                "client_identity_digest": client_digest,
                "release_binding_digest": binding_digest,
                "image_reference": image_reference,
                "image_id": image_id,
                "peer_argv": list(peer_argv),
            }
            if any(owner[name] != value for name, value in immutable.items()) or (
                nonce is not None and owner["nonce"] != nonce
            ):
                raise NetworkControlError("network_control_owner_mismatch")
            return cls(client, state_root, owner)
        nonce = nonce or secrets.token_hex(32)
        if type(nonce) is not str or _HEX64_RE.fullmatch(nonce) is None:
            raise NetworkControlError("network_control_input_invalid")
        owner_digest = _digest(
            b"PICO_NETWORK_CONTROL_OWNER_V1\0",
            {
                "client_identity_digest": client_digest,
                "release_binding_digest": binding_digest,
                "nonce": nonce,
                "image_reference": image_reference,
                "image_id": image_id,
                "peer_argv": list(peer_argv),
            },
        )
        short = owner_digest[7:19]
        labels = {
            "io.pico.network-control.managed": "true",
            "io.pico.network-control.nonce": nonce,
            "io.pico.network-control.owner": owner_digest,
        }
        owner = {
            "record_type": "docker_sandbox_network_control_owner",
            "format_version": FORMAT_VERSION,
            "phase": "planned",
            "owner_digest": owner_digest,
            "client_identity_digest": client_digest,
            "release_binding_digest": binding_digest,
            "nonce": nonce,
            "labels": labels,
            "image_reference": image_reference,
            "image_id": image_id,
            "peer_argv": list(peer_argv),
            "network_name": "pico-network-control-" + short,
            "network_id": "",
            "peer_name": "pico-network-peer-" + short,
            "peer_id": "",
        }
        cls._validate_owner(owner)
        instance = cls(client, state_root, owner)
        instance._write_owner()
        return instance

    @staticmethod
    def _read_owner(state_root, path):
        try:
            raw = securitylib.read_private_bytes(
                path,
                trusted_root=state_root,
                trusted_root_identity=securitylib.private_directory_identity(
                    state_root
                ),
                max_bytes=MAX_OWNER_BYTES,
            )
            owner = _decode_json(raw)
            NetworkControl._validate_owner(owner)
            return owner
        except NetworkControlError:
            raise
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise NetworkControlError("network_control_state_invalid") from exc

    @staticmethod
    def _validate_owner(owner):
        if (
            not isinstance(owner, dict)
            or set(owner) != _OWNER_FIELDS
            or owner["record_type"] != "docker_sandbox_network_control_owner"
            or owner["format_version"] != FORMAT_VERSION
            or owner["phase"]
            not in {"planned", "network_created", "peer_created", "active", "cleanup_pending", "cleaned"}
            or any(
                not _valid_digest(owner[name])
                for name in (
                    "owner_digest",
                    "client_identity_digest",
                    "release_binding_digest",
                    "image_reference",
                    "image_id",
                )
            )
            or type(owner["nonce"]) is not str
            or _HEX64_RE.fullmatch(owner["nonce"]) is None
            or not _valid_name(owner["network_name"])
            or not _valid_name(owner["peer_name"])
            or owner["network_id"] not in {""} and not _valid_id(owner["network_id"])
            or owner["peer_id"] not in {""} and not _valid_id(owner["peer_id"])
            or not isinstance(owner["peer_argv"], list)
            or not owner["peer_argv"]
            or any(
                type(item) is not str or not item or "\x00" in item
                for item in owner["peer_argv"]
            )
            or not isinstance(owner["labels"], dict)
            or owner["labels"]
            != {
                "io.pico.network-control.managed": "true",
                "io.pico.network-control.nonce": owner["nonce"],
                "io.pico.network-control.owner": owner["owner_digest"],
            }
        ):
            raise NetworkControlError("network_control_state_invalid")
        expected_digest = _digest(
            b"PICO_NETWORK_CONTROL_OWNER_V1\0",
            {
                "client_identity_digest": owner["client_identity_digest"],
                "release_binding_digest": owner["release_binding_digest"],
                "nonce": owner["nonce"],
                "image_reference": owner["image_reference"],
                "image_id": owner["image_id"],
                "peer_argv": owner["peer_argv"],
            },
        )
        short = expected_digest[7:19]
        network_id = owner["network_id"]
        peer_id = owner["peer_id"]
        phase = owner["phase"]
        if (
            owner["owner_digest"] != expected_digest
            or owner["network_name"] != "pico-network-control-" + short
            or owner["peer_name"] != "pico-network-peer-" + short
            or phase in {"planned", "cleaned"}
            and bool(network_id or peer_id)
            or phase == "network_created"
            and (not network_id or bool(peer_id))
            or phase in {"peer_created", "active"}
            and (not network_id or not peer_id)
            or phase == "cleanup_pending"
            and bool(peer_id)
            and not network_id
        ):
            raise NetworkControlError("network_control_state_invalid")

    def _write_owner(self):
        self._validate_owner(self.owner)
        try:
            securitylib.write_private_bytes_atomic(
                self.owner_path,
                _canonical_json(self.owner) + b"\n",
                trusted_root=self.state_root,
                trusted_root_identity=self._root_identity,
                max_existing_bytes=MAX_OWNER_BYTES,
            )
        except (OSError, ValueError) as exc:
            raise NetworkControlError("network_control_state_invalid") from exc

    def _transition(self, phase, **changes):
        self.owner = {**self.owner, **changes, "phase": phase}
        self._write_owner()

    def _find_exact(self, kind, verifier):
        ids = _list_ids(self.client, kind, self.owner["labels"])
        matches = []
        for object_id in ids:
            payload = _inspect_json(self.client, kind, object_id)
            try:
                verifier(payload, object_id)
            except NetworkControlError:
                continue
            matches.append(object_id)
        if len(matches) > 1:
            raise NetworkControlError("network_control_inventory_ambiguous")
        return matches[0] if matches else ""

    def _verify_network(self, payload, network_id):
        try:
            ipam = payload["IPAM"]
            config = ipam["Config"]
            valid = (
                payload["Id"] == network_id
                and payload["Name"] == self.owner["network_name"]
                and payload["Driver"] == "bridge"
                and payload["Scope"] == "local"
                and payload["Internal"] is False
                and payload["Attachable"] is False
                and payload["Ingress"] is False
                and payload["Labels"] == self.owner["labels"]
                and ipam["Driver"] == "default"
                and ipam.get("Options") in (None, {})
                and isinstance(config, list)
                and len(config) == 1
                and isinstance(config[0].get("Subnet"), str)
                and isinstance(config[0].get("Gateway"), str)
                and config[0]["Subnet"]
                and config[0]["Gateway"]
            )
        except (KeyError, IndexError, TypeError):
            valid = False
        if not valid:
            raise NetworkControlError("network_control_identity_mismatch")

    @property
    def _peer_alias(self):
        return "pico-peer-" + self.owner["owner_digest"][7:19]

    def _verify_peer(self, payload, peer_id, *, require_running=False):
        try:
            host = payload["HostConfig"]
            config = payload["Config"]
            descriptor = payload.get("ImageManifestDescriptor", {})
            networks = payload["NetworkSettings"]["Networks"]
            attachment = networks[self.owner["network_name"]]
            labels = config["Labels"]
            attachment_ready = (
                attachment["NetworkID"] == self.owner["network_id"]
                and isinstance(attachment["IPAddress"], str)
                and bool(attachment["IPAddress"])
                and isinstance(attachment["Gateway"], str)
                and bool(attachment["Gateway"])
            )
            attachment_pending = (
                attachment["NetworkID"] == ""
                and attachment["IPAddress"] == ""
                and attachment["Gateway"] == ""
            )
            valid = (
                payload["Id"] == peer_id
                and payload["Name"] == "/" + self.owner["peer_name"]
                and payload["Image"] == self.owner["image_reference"]
                and descriptor.get("digest") == self.owner["image_reference"]
                and descriptor.get("annotations", {}).get("config.digest")
                == self.owner["image_id"]
                and config["Image"] == self.owner["image_reference"]
                and isinstance(labels, dict)
                and all(
                    labels.get(key) == value
                    for key, value in self.owner["labels"].items()
                )
                and not any(
                    key.startswith("io.pico.network-control.")
                    and key not in self.owner["labels"]
                    for key in labels
                )
                and config["Cmd"] == self.owner["peer_argv"]
                and config.get("Entrypoint") in (None, [])
                and host["NetworkMode"] == self.owner["network_id"]
                and host["ExtraHosts"] == ["pico-network-host:host-gateway"]
                and host.get("Binds") is None
                and host["Privileged"] is False
                and host["ReadonlyRootfs"] is True
                and host.get("CapAdd") in (None, [])
                and host["CapDrop"] == ["ALL"]
                and host["SecurityOpt"] == ["no-new-privileges:true"]
                and host["PortBindings"] == {}
                and host["PublishAllPorts"] is False
                and payload.get("Mounts") == []
                and set(networks) == {self.owner["network_name"]}
                and (attachment_ready or attachment_pending)
                and attachment.get("Aliases") == [self._peer_alias]
                and (
                    not require_running
                    or payload["State"]["Running"] is True
                    and attachment_ready
                )
            )
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise NetworkControlError("network_control_identity_mismatch")

    def _resolve_created(self, kind, result, verifier, error_code):
        object_id = _parse_id(result)
        if object_id:
            try:
                verifier(_inspect_json(self.client, kind, object_id), object_id)
                return object_id
            except NetworkControlError as exc:
                if exc.code == "network_control_identity_mismatch":
                    raise
        try:
            recovered = self._find_exact(kind, verifier)
        except NetworkControlError:
            raise
        if not recovered:
            raise NetworkControlError(error_code)
        return recovered

    def _create_network(self):
        self._transition("planned", network_id="", peer_id="")
        argv = ["network", "create", "--driver=bridge"]
        argv.extend(
            f"--label={key}={value}" for key, value in sorted(self.owner["labels"].items())
        )
        argv.append(self.owner["network_name"])
        try:
            result = self.client.command(
                argv, timeout=30, max_bytes=MAX_DOCKER_RESPONSE_BYTES
            )
        except Exception:
            try:
                network_id = self._find_exact("network", self._verify_network)
            except BaseException:
                raise
            if not network_id:
                raise
        else:
            network_id = self._resolve_created(
                "network",
                result,
                self._verify_network,
                "network_control_network_create_failed",
            )
        self._transition("network_created", network_id=network_id)

    def _create_peer(self):
        self._transition("network_created", peer_id="")
        argv = [
            "container",
            "create",
            "--pull=never",
            "--name=" + self.owner["peer_name"],
            "--network=" + self.owner["network_id"],
            "--network-alias=" + self._peer_alias,
            "--add-host=pico-network-host:host-gateway",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
        ]
        argv.extend(
            f"--label={key}={value}" for key, value in sorted(self.owner["labels"].items())
        )
        argv.append(self.owner["image_reference"])
        argv.extend(self.owner["peer_argv"])
        try:
            result = self.client.command(
                argv, timeout=30, max_bytes=MAX_DOCKER_RESPONSE_BYTES
            )
        except Exception:
            try:
                peer_id = self._find_exact("container", self._verify_peer)
            except BaseException:
                raise
            if not peer_id:
                raise
        else:
            peer_id = self._resolve_created(
                "container",
                result,
                self._verify_peer,
                "network_control_peer_create_failed",
            )
        self._transition("peer_created", peer_id=peer_id)

    def prepare(self):
        if self.owner["phase"] == "cleaned":
            raise NetworkControlError("network_control_state_invalid")
        if not self.owner["network_id"]:
            self._create_network()
        self._verify_network(
            _inspect_json(self.client, "network", self.owner["network_id"]),
            self.owner["network_id"],
        )
        if not self.owner["peer_id"]:
            self._create_peer()
        peer = _inspect_json(self.client, "container", self.owner["peer_id"])
        self._verify_peer(peer, self.owner["peer_id"])
        if peer.get("State", {}).get("Running") is not True:
            started = self.client.command(
                ["container", "start", self.owner["peer_id"]],
                timeout=30,
                max_bytes=MAX_DOCKER_RESPONSE_BYTES,
            )
            if not _clean_result(started):
                raise NetworkControlError("network_control_peer_start_failed")
            peer = _inspect_json(self.client, "container", self.owner["peer_id"])
            if peer.get("State", {}).get("Running") is not True:
                raise NetworkControlError("network_control_peer_start_failed")
        self._verify_peer(peer, self.owner["peer_id"], require_running=True)
        attachment = peer["NetworkSettings"]["Networks"][self.owner["network_name"]]
        topology_payload = {
            "owner_digest": self.owner["owner_digest"],
            "network_id": self.owner["network_id"],
            "peer_id": self.owner["peer_id"],
            "peer_alias": self._peer_alias,
            "peer_ipv4": attachment["IPAddress"],
            "gateway_ipv4": attachment["Gateway"],
        }
        topology = NetworkControlTopology(
            topology_digest=_digest(
                b"PICO_NETWORK_CONTROL_TOPOLOGY_V1\0", topology_payload
            ),
            peer_alias=self._peer_alias,
            peer_ipv4=attachment["IPAddress"],
            gateway_ipv4=attachment["Gateway"],
        )
        self._transition("active")
        return topology

    def _identity_for_cleanup(self, kind, object_id):
        payload = _inspect_json(self.client, kind, object_id)
        if kind == "container":
            self._verify_peer(payload, object_id)
        else:
            self._verify_network(payload, object_id)

    def _remove_owned(self, kind, object_id):
        self._identity_for_cleanup(kind, object_id)
        args = (
            ["container", "rm", "--force", object_id]
            if kind == "container"
            else ["network", "rm", object_id]
        )
        result = self.client.command(
            args, timeout=30, max_bytes=MAX_DOCKER_RESPONSE_BYTES
        )
        if not _clean_result(result):
            return False
        return not _list_ids(self.client, kind, {}, object_id=object_id)

    def cleanup(self):
        try:
            self._transition("cleanup_pending")
            owned_containers = _list_ids(
                self.client, "container", self.owner["labels"]
            )
            owned_networks = _list_ids(self.client, "network", self.owner["labels"])
            if self.owner["peer_id"] and self.owner["peer_id"] not in owned_containers:
                if _list_ids(
                    self.client, "container", {}, object_id=self.owner["peer_id"]
                ):
                    raise NetworkControlError("network_control_identity_mismatch")
            if self.owner["network_id"] and self.owner["network_id"] not in owned_networks:
                if _list_ids(
                    self.client, "network", {}, object_id=self.owner["network_id"]
                ):
                    raise NetworkControlError("network_control_identity_mismatch")
            if len(owned_containers) > 1 or len(owned_networks) > 1:
                raise NetworkControlError("network_control_inventory_ambiguous")
            if owned_containers and not self._remove_owned(
                "container", owned_containers[0]
            ):
                raise NetworkControlError("network_control_cleanup_failed")
            if owned_networks and not self._remove_owned("network", owned_networks[0]):
                raise NetworkControlError("network_control_cleanup_failed")
            containers_remaining = len(
                _list_ids(self.client, "container", self.owner["labels"])
            )
            networks_remaining = len(
                _list_ids(self.client, "network", self.owner["labels"])
            )
            if containers_remaining or networks_remaining:
                raise NetworkControlError("network_control_cleanup_failed")
            self._transition("cleaned", peer_id="", network_id="")
            return NetworkControlCleanup("completed", True, 0, 0)
        except NetworkControlError:
            try:
                self._transition("cleanup_pending")
            except NetworkControlError:
                pass
            try:
                containers_remaining = len(
                    _list_ids(self.client, "container", self.owner["labels"])
                )
                networks_remaining = len(
                    _list_ids(self.client, "network", self.owner["labels"])
                )
                inventory_complete = True
            except NetworkControlError:
                containers_remaining = int(bool(self.owner["peer_id"]))
                networks_remaining = int(bool(self.owner["network_id"]))
                inventory_complete = False
            return NetworkControlCleanup(
                "failed",
                inventory_complete,
                containers_remaining,
                networks_remaining,
            )

    def run(self, positive_probe, production_probe):
        facts = {name: False for name in sorted(_FACT_FIELDS)}
        topology = None
        endpoints = None
        try:
            topology = self.prepare()
            positive = positive_probe(topology, self.owner["nonce"])
            if not isinstance(positive, NetworkControlPositiveFacts):
                raise NetworkControlError("network_control_probe_invalid")
            endpoints = _strict_endpoint(positive.endpoints, topology)
            negative = production_probe(endpoints, self.owner["nonce"])
            endpoints, facts = _strict_facts(
                positive, negative, topology, self.owner["nonce"]
            )
        except NetworkControlError:
            self.cleanup()
            raise
        except BaseException:
            self.cleanup()
            raise
        cleanup = self.cleanup()
        return _result(
            self.owner["owner_digest"],
            topology.topology_digest,
            endpoints.endpoint_digest,
            facts,
            cleanup,
        )
