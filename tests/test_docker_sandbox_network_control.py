from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from pico.sandbox.network_control import (
    NetworkControl,
    NetworkControlEndpoints,
    NetworkControlError,
    NetworkControlNegativeFacts,
    NetworkControlPositiveFacts,
    validate_network_control_result,
)


NETWORK_ID = "a" * 64
CONTAINER_ID = "b" * 64
FOREIGN_NETWORK_ID = "c" * 64
FOREIGN_CONTAINER_ID = "d" * 64
IMAGE_REFERENCE = "sha256:" + "e" * 64
IMAGE_ID = "sha256:" + "f" * 64
CLIENT_DIGEST = "sha256:" + "1" * 64
NONCE = "2" * 64
PEER_ARGV = ("/usr/bin/python3", "-c", "raise SystemExit(0)")


def _release_binding():
    return {
        "status": "bound",
        "expected_manifest_digest": "sha256:" + "3" * 64,
        "release_nonce": "4" * 64,
        "job_id": "d7-darwin-arm64-clean-01",
        "commit": "5" * 40,
        "sdist_sha256": "sha256:" + "6" * 64,
        "run_kind": "clean",
        "run_index": 1,
    }


def _result(
    *,
    exit_code=0,
    stdout=b"",
    stderr=b"",
    timed_out=False,
    stdout_truncated=False,
    stderr_truncated=False,
):
    return SimpleNamespace(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _labels(argv):
    return {
        item.removeprefix("--label=").split("=", 1)[0]: item.split("=", 2)[2]
        for item in argv
        if item.startswith("--label=")
    }


def _name(argv):
    return next(item.split("=", 1)[1] for item in argv if item.startswith("--name="))


def _matches_filters(labels, argv):
    filters = [
        argv[index + 1]
        for index, item in enumerate(argv[:-1])
        if item == "--filter"
    ]
    for value in filters:
        if not value.startswith("label="):
            continue
        key, expected = value.removeprefix("label=").split("=", 1)
        if labels.get(key) != expected:
            return False
    return True


class FakeDockerClient:
    def __init__(self, *faults):
        self.faults = set(faults)
        self.commands = []
        self.networks = {}
        self.containers = {}
        self.removed = []

    def identity_digest(self):
        return CLIENT_DIGEST

    def command(self, argv, **_kwargs):
        argv = list(argv)
        self.commands.append(argv)
        if argv[:2] == ["network", "create"]:
            return self._create_network(argv)
        if argv[:2] == ["network", "inspect"]:
            return self._inspect_network(argv[2])
        if argv[:2] == ["network", "ls"]:
            return self._list_networks(argv)
        if argv[:2] == ["network", "rm"]:
            return self._remove_network(argv[2])
        if argv[:2] == ["container", "create"]:
            return self._create_container(argv)
        if argv[:2] == ["container", "inspect"]:
            return self._inspect_container(argv[2])
        if argv[:3] == ["container", "ls", "--all"]:
            return self._list_containers(argv)
        if argv[:2] == ["container", "start"]:
            self.containers[argv[2]]["running"] = True
            return _result(stdout=(argv[2] + "\n").encode("ascii"))
        if argv[:3] == ["container", "rm", "--force"]:
            return self._remove_container(argv[3])
        raise AssertionError(f"unexpected Docker argv: {argv!r}")

    def _create_network(self, argv):
        if "network_create_no_object" not in self.faults:
            self.networks[NETWORK_ID] = {
                "name": argv[-1],
                "labels": _labels(argv),
            }
        if "network_create_lost" in self.faults:
            raise RuntimeError("network create response lost")
        stdout = b"not-an-id\n" if "network_create_malformed" in self.faults else (NETWORK_ID + "\n").encode("ascii")
        return _result(stdout=stdout)

    def _create_container(self, argv):
        alias = next(
            item.split("=", 1)[1]
            for item in argv
            if item.startswith("--network-alias=")
        )
        self.containers[CONTAINER_ID] = {
            "name": _name(argv),
            "labels": _labels(argv),
            "image_reference": IMAGE_REFERENCE,
            "image_id": IMAGE_ID,
            "argv": list(PEER_ARGV),
            "network_id": NETWORK_ID,
            "network_name": self.networks[NETWORK_ID]["name"],
            "alias": alias,
            "running": False,
        }
        if "peer_create_lost" in self.faults:
            raise RuntimeError("peer create response lost")
        stdout = b"not-an-id\n" if "peer_create_malformed" in self.faults else (CONTAINER_ID + "\n").encode("ascii")
        return _result(stdout=stdout)

    def _network_payload(self, network_id):
        value = self.networks[network_id]
        labels = value["labels"]
        if "network_identity_mismatch" in self.faults:
            labels = {**labels, "foreign": "true"}
        return {
            "Name": value["name"],
            "Id": network_id,
            "Driver": "bridge",
            "Scope": "local",
            "Internal": False,
            "Attachable": False,
            "Ingress": False,
            "IPAM": {
                "Driver": "default",
                "Options": {},
                "Config": [
                    {"Subnet": "172.28.0.0/16", "Gateway": "172.28.0.1"}
                ],
            },
            "Labels": labels,
        }

    def _container_payload(self, container_id):
        value = self.containers[container_id]
        image_reference = value["image_reference"]
        if "peer_identity_mismatch" in self.faults:
            image_reference = "sha256:" + "0" * 64
        labels = {
            **value["labels"],
            "io.pico.sandbox.managed": "true",
            "org.opencontainers.image.title": "Pico Docker Sandbox",
        }
        if "peer_control_label_mismatch" in self.faults:
            labels["io.pico.network-control.foreign"] = "true"
        network_id = value["network_id"] if value["running"] else ""
        address = "172.28.0.2" if value["running"] else ""
        gateway = "172.28.0.1" if value["running"] else ""
        return {
            "Id": container_id,
            "Name": "/" + value["name"],
            "Image": image_reference,
            "ImageManifestDescriptor": {
                "digest": value["image_reference"],
                "annotations": {"config.digest": value["image_id"]},
            },
            "Config": {
                "Image": value["image_reference"],
                "Labels": labels,
                "Cmd": value["argv"],
                "Entrypoint": None,
            },
            "HostConfig": {
                "NetworkMode": value["network_id"],
                "ExtraHosts": ["pico-network-host:host-gateway"],
                "Binds": None,
                "Privileged": False,
                "ReadonlyRootfs": True,
                "CapAdd": None,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PortBindings": {},
                "PublishAllPorts": False,
            },
            "Mounts": [],
            "State": {"Running": value["running"]},
            "NetworkSettings": {
                "Networks": {
                    value["network_name"]: {
                        "NetworkID": network_id,
                        "IPAddress": address,
                        "Gateway": gateway,
                        "Aliases": [value["alias"]],
                    }
                }
            },
        }

    def _inspect_network(self, network_id):
        if network_id not in self.networks:
            return _result(exit_code=1, stderr=b"not found")
        return _result(stdout=json.dumps(self._network_payload(network_id)).encode())

    def _inspect_container(self, container_id):
        if container_id not in self.containers:
            return _result(exit_code=1, stderr=b"not found")
        return _result(stdout=json.dumps(self._container_payload(container_id)).encode())

    def _list_networks(self, argv):
        if "inventory_truncated" in self.faults:
            return _result(stdout=b"x", stdout_truncated=True)
        identifier = next(
            (
                item.removeprefix("id=")
                for index, item in enumerate(argv)
                if index and argv[index - 1] == "--filter" and item.startswith("id=")
            ),
            None,
        )
        values = [
            network_id
            for network_id, value in self.networks.items()
            if (identifier is None or network_id == identifier)
            and _matches_filters(value["labels"], argv)
        ]
        return _result(stdout=("\n".join(values) + ("\n" if values else "")).encode())

    def _list_containers(self, argv):
        if "inventory_truncated" in self.faults:
            return _result(stdout=b"x", stdout_truncated=True)
        identifier = next(
            (
                item.removeprefix("id=")
                for index, item in enumerate(argv)
                if index and argv[index - 1] == "--filter" and item.startswith("id=")
            ),
            None,
        )
        values = [
            container_id
            for container_id, value in self.containers.items()
            if (identifier is None or container_id == identifier)
            and _matches_filters(value["labels"], argv)
        ]
        return _result(stdout=("\n".join(values) + ("\n" if values else "")).encode())

    def _remove_container(self, container_id):
        if "container_cleanup_failed" in self.faults:
            return _result(exit_code=1, stderr=b"failed")
        if container_id in self.containers:
            del self.containers[container_id]
            self.removed.append(("container", container_id))
        return _result(stdout=(container_id + "\n").encode("ascii"))

    def _remove_network(self, network_id):
        if "network_cleanup_failed" in self.faults:
            return _result(exit_code=1, stderr=b"failed")
        if network_id in self.networks:
            del self.networks[network_id]
            self.removed.append(("network", network_id))
        return _result(stdout=(network_id + "\n").encode("ascii"))

    def add_foreign_objects(self):
        self.networks[FOREIGN_NETWORK_ID] = {
            "name": "foreign-network",
            "labels": {"io.pico.network-control.managed": "true"},
        }
        self.containers[FOREIGN_CONTAINER_ID] = {
            "name": "foreign-container",
            "labels": {"io.pico.network-control.managed": "true"},
            "image_reference": IMAGE_REFERENCE,
            "image_id": IMAGE_ID,
            "argv": list(PEER_ARGV),
            "network_id": FOREIGN_NETWORK_ID,
            "network_name": "foreign-network",
            "alias": "foreign-peer",
            "running": True,
        }


def _control(tmp_path, client):
    return NetworkControl.open(
        client,
        tmp_path / "network-control",
        release_binding=_release_binding(),
        image_reference=IMAGE_REFERENCE,
        image_id=IMAGE_ID,
        peer_argv=PEER_ARGV,
        nonce=NONCE,
    )


def _rewrite_owner(root, mutate):
    path = root / "network-control" / "owner.json"
    owner = json.loads(path.read_text(encoding="ascii"))
    mutate(owner)
    path.write_text(json.dumps(owner, sort_keys=True), encoding="ascii")


def _endpoints(topology):
    return NetworkControlEndpoints(
        topology_digest=topology.topology_digest,
        peer_alias=topology.peer_alias,
        peer_ipv4=topology.peer_ipv4,
        peer_tcp_port=32101,
        peer_udp_port=32102,
        gateway_ipv4=topology.gateway_ipv4,
        gateway_tcp_port=32103,
        host_address="host.docker.internal",
        host_tcp_port=32104,
        guest_tcp_port=32105,
    )


def _positive(topology, nonce):
    return NetworkControlPositiveFacts(
        endpoints=_endpoints(topology),
        challenge_nonce=nonce,
        peer_dns_reachable=True,
        peer_tcp_reachable=True,
        peer_udp_reachable=True,
        gateway_reachable=True,
        host_reachable=True,
    )


def _negative(endpoints, _nonce):
    return NetworkControlNegativeFacts(
        endpoint_digest=endpoints.endpoint_digest,
        peer_dns_denied=True,
        peer_tcp_denied=True,
        peer_udp_denied=True,
        gateway_denied=True,
        host_denied=True,
        host_to_guest_denied=True,
    )


def test_network_control_runs_same_endpoint_controls_and_emits_sanitized_result(
    tmp_path,
):
    client = FakeDockerClient()
    control = _control(tmp_path, client)

    result = control.run(_positive, _negative)

    assert validate_network_control_result(result) is result
    create = next(
        argv for argv in client.commands if argv[:2] == ["container", "create"]
    )
    assert "--add-host=pico-network-host:host-gateway" in create
    assert result["status"] == "passed"
    assert result["reason_code"] == "verified"
    assert result["facts"] == {
        "control_peer_tcp_reachable": True,
        "control_peer_udp_reachable": True,
        "control_peer_dns_reachable": True,
        "control_gateway_reachable": True,
        "control_host_reachable": True,
        "production_peer_tcp_denied": True,
        "production_peer_udp_denied": True,
        "production_peer_dns_denied": True,
        "production_gateway_denied": True,
        "production_host_denied": True,
        "host_to_guest_denied": True,
    }
    assert result["cleanup"]["status"] == "completed"
    assert result["cleanup"]["containers_remaining"] == 0
    assert result["cleanup"]["networks_remaining"] == 0
    assert not client.networks
    assert not client.containers
    encoded = json.dumps(result, sort_keys=True)
    assert "172.28." not in encoded
    assert "host.docker.internal" not in encoded
    assert NONCE not in encoded
    assert (tmp_path / "network-control" / "owner.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "fault",
    (
        "network_create_malformed",
        "network_create_lost",
        "peer_create_malformed",
        "peer_create_lost",
    ),
)
def test_create_lost_or_malformed_response_recovers_from_exact_inventory(
    tmp_path,
    fault,
):
    client = FakeDockerClient(fault)

    result = _control(tmp_path, client).run(_positive, _negative)

    assert result["status"] == "passed"
    assert client.removed == [
        ("container", CONTAINER_ID),
        ("network", NETWORK_ID),
    ]


def test_malformed_create_without_inventory_proof_fails_closed(tmp_path):
    client = FakeDockerClient("network_create_malformed", "network_create_no_object")
    control = _control(tmp_path, client)

    with pytest.raises(NetworkControlError, match="network_control_network_create_failed"):
        control.run(_positive, _negative)

    assert not client.removed


@pytest.mark.parametrize(
    "fault",
    (
        "network_identity_mismatch",
        "peer_identity_mismatch",
        "peer_control_label_mismatch",
    ),
)
def test_identity_mismatch_is_never_deleted(tmp_path, fault):
    client = FakeDockerClient(fault)
    control = _control(tmp_path, client)

    with pytest.raises(NetworkControlError, match="network_control_identity_mismatch"):
        control.run(_positive, _negative)

    assert not client.removed
    assert client.networks


def test_cleanup_failure_is_durable_and_retryable_by_full_id(tmp_path):
    client = FakeDockerClient("container_cleanup_failed")
    root = tmp_path / "network-control"

    result = _control(tmp_path, client).run(_positive, _negative)

    assert result["status"] == "failed"
    assert result["reason_code"] == "network_control_cleanup_failed"
    assert result["cleanup"]["status"] == "failed"
    assert client.containers
    client.faults.remove("container_cleanup_failed")

    recovered = NetworkControl.open(
        client,
        root,
        release_binding=_release_binding(),
        image_reference=IMAGE_REFERENCE,
        image_id=IMAGE_ID,
        peer_argv=PEER_ARGV,
    )
    cleanup = recovered.cleanup()

    assert cleanup.status == "completed"
    assert cleanup.containers_remaining == 0
    assert cleanup.networks_remaining == 0
    assert client.removed[-2:] == [
        ("container", CONTAINER_ID),
        ("network", NETWORK_ID),
    ]


def test_truncated_inventory_blocks_cleanup_until_retry(tmp_path):
    client = FakeDockerClient()
    control = _control(tmp_path, client)
    control.prepare()
    client.faults.add("inventory_truncated")

    failed = control.cleanup()

    assert failed.status == "failed"
    assert failed.inventory_complete is False
    assert not client.removed
    client.faults.remove("inventory_truncated")
    completed = control.cleanup()
    assert completed.status == "completed"


def test_base_exception_preserves_primary_and_cleans_owned_objects(tmp_path):
    client = FakeDockerClient()
    control = _control(tmp_path, client)
    primary = KeyboardInterrupt("stop")

    def interrupt(_endpoints, _nonce):
        raise primary

    with pytest.raises(KeyboardInterrupt) as caught:
        control.run(_positive, interrupt)

    assert caught.value is primary
    assert not client.networks
    assert not client.containers
    assert NetworkControl.open(
        client,
        tmp_path / "network-control",
        release_binding=_release_binding(),
        image_reference=IMAGE_REFERENCE,
        image_id=IMAGE_ID,
        peer_argv=PEER_ARGV,
    ).cleanup().status == "completed"


def test_foreign_objects_are_not_selected_or_removed(tmp_path):
    client = FakeDockerClient()
    client.add_foreign_objects()

    result = _control(tmp_path, client).run(_positive, _negative)

    assert result["status"] == "passed"
    assert set(client.networks) == {FOREIGN_NETWORK_ID}
    assert set(client.containers) == {FOREIGN_CONTAINER_ID}
    assert ("network", FOREIGN_NETWORK_ID) not in client.removed
    assert ("container", FOREIGN_CONTAINER_ID) not in client.removed


def test_probe_facts_must_be_typed_and_bound_to_same_endpoint(tmp_path):
    client = FakeDockerClient()
    control = _control(tmp_path, client)

    with pytest.raises(NetworkControlError, match="network_control_probe_invalid"):
        control.run(lambda _topology, _nonce: deepcopy({}), _negative)

    assert not client.networks
    assert not client.containers

    client = FakeDockerClient()
    control = NetworkControl.open(
        client,
        tmp_path / "second-network-control",
        release_binding=_release_binding(),
        image_reference=IMAGE_REFERENCE,
        image_id=IMAGE_ID,
        peer_argv=PEER_ARGV,
        nonce="7" * 64,
    )

    def wrong_endpoint(_endpoints, _nonce):
        return NetworkControlNegativeFacts(
            endpoint_digest="sha256:" + "0" * 64,
            peer_dns_denied=True,
            peer_tcp_denied=True,
            peer_udp_denied=True,
            gateway_denied=True,
            host_denied=True,
            host_to_guest_denied=True,
        )

    with pytest.raises(NetworkControlError, match="network_control_probe_invalid"):
        control.run(_positive, wrong_endpoint)
    assert not client.networks
    assert not client.containers


@pytest.mark.parametrize(
    "mutate",
    (
        lambda owner: (
            owner.__setitem__("nonce", "9" * 64),
            owner["labels"].__setitem__(
                "io.pico.network-control.nonce",
                "9" * 64,
            ),
        ),
        lambda owner: (
            owner.__setitem__("owner_digest", "sha256:" + "9" * 64),
            owner["labels"].__setitem__(
                "io.pico.network-control.owner",
                "sha256:" + "9" * 64,
            ),
            owner.__setitem__("network_name", "pico-network-control-999999999999"),
            owner.__setitem__("peer_name", "pico-network-peer-999999999999"),
        ),
        lambda owner: owner.__setitem__("network_name", "pico-network-control-tampered"),
        lambda owner: owner.__setitem__("peer_name", "pico-network-peer-tampered"),
    ),
)
def test_reopen_recomputes_owner_identity_and_derived_names(tmp_path, mutate):
    client = FakeDockerClient()
    _control(tmp_path, client)
    _rewrite_owner(tmp_path, mutate)

    with pytest.raises(NetworkControlError, match="network_control_state_invalid"):
        _control(tmp_path, client)


@pytest.mark.parametrize(
    "phase,network_id,peer_id",
    (
        ("planned", NETWORK_ID, ""),
        ("network_created", "", ""),
        ("network_created", NETWORK_ID, CONTAINER_ID),
        ("peer_created", NETWORK_ID, ""),
        ("active", "", CONTAINER_ID),
        ("cleanup_pending", "", CONTAINER_ID),
        ("cleaned", NETWORK_ID, ""),
    ),
)
def test_reopen_rejects_impossible_phase_and_id_combinations(
    tmp_path,
    phase,
    network_id,
    peer_id,
):
    client = FakeDockerClient()
    _control(tmp_path, client)
    _rewrite_owner(
        tmp_path,
        lambda owner: owner.update(
            phase=phase,
            network_id=network_id,
            peer_id=peer_id,
        ),
    )

    with pytest.raises(NetworkControlError, match="network_control_state_invalid"):
        _control(tmp_path, client)


@pytest.mark.parametrize(
    "network_id,peer_id",
    (("", ""), (NETWORK_ID, ""), (NETWORK_ID, CONTAINER_ID)),
)
def test_cleanup_pending_accepts_only_recoverable_id_prefixes(
    tmp_path,
    network_id,
    peer_id,
):
    client = FakeDockerClient()
    _control(tmp_path, client)
    _rewrite_owner(
        tmp_path,
        lambda owner: owner.update(
            phase="cleanup_pending",
            network_id=network_id,
            peer_id=peer_id,
        ),
    )

    reopened = _control(tmp_path, client)

    assert reopened.owner["network_id"] == network_id
    assert reopened.owner["peer_id"] == peer_id
