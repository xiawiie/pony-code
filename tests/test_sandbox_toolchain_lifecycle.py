from datetime import datetime, timezone

from pico.sandbox_lifecycle import UnknownIdentityError, apply_prune_plan, plan_prune


def retirement():
    return {
        "security_reason": "security",
        "replacement": "current",
        "compatibility_evidence": "artifact://compatibility",
        "rollback_window": "2026-01-01T00:00:00Z",
        "release_note": "release-note",
    }


def test_prune_keeps_active_and_referenced_bundles(tmp_path):
    inventory = [
        {"path": str(tmp_path / "current"), "identity": "current", "verified": True},
        {"path": str(tmp_path / "active"), "identity": "active", "verified": True, "active_lock": True},
        {"path": str(tmp_path / "recent"), "identity": "recent", "verified": True, "recent": True},
        {"path": str(tmp_path / "referenced"), "identity": "referenced", "verified": True, "referenced": True},
        {"path": str(tmp_path / "old"), "identity": "old", "verified": True, "retirement": retirement()},
    ]
    plan = plan_prune(
        inventory,
        pinned_identity="current",
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    assert [item["identity"] for item in plan["keep"]] == ["current", "active", "recent", "referenced"]
    assert [item["identity"] for item in plan["delete"]] == ["old"]


def test_apply_prune_rejects_symlink_source(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    plan = {
        "pinned_identity": "current",
        "delete": [
            {
                "path": str(link),
                "identity": "old",
                "verified": True,
                "device": real.stat().st_dev,
                "inode": real.stat().st_ino,
            }
        ],
    }
    try:
        apply_prune_plan(
            plan, trash_root=tmp_path / "trash", allowed_roots=(tmp_path,)
        )
    except UnknownIdentityError:
        pass
    else:
        raise AssertionError("unsafe bundle path must not be pruned")


def test_apply_prune_rejects_directory_swapped_after_plan(tmp_path):
    bundles = tmp_path / "bundles"
    source = bundles / "old"
    source.mkdir(parents=True)
    info = source.stat()
    plan = {
        "pinned_identity": "current",
        "delete": [
            {
                "path": str(source),
                "identity": "old",
                "verified": True,
                "device": info.st_dev,
                "inode": info.st_ino,
            }
        ],
    }
    source.rename(bundles / "moved")
    source.mkdir()

    try:
        apply_prune_plan(
            plan,
            trash_root=tmp_path / "trash",
            allowed_roots=(bundles,),
        )
    except UnknownIdentityError:
        pass
    else:
        raise AssertionError("changed bundle identity must not be pruned")

    assert source.exists()


def test_toolchain_inventory_marks_unknown_and_trusted_retired_bundles(tmp_path):
    from pico.sandbox_lifecycle import bundle_tree_hash
    from pico.sandbox_toolchain import SandboxToolchain

    root = tmp_path / "toolchain"
    current = "linux-x64-node-current-srt-current"
    old = "linux-x64-node-old-srt-old"
    old_dir = root / "bundles" / old
    node = old_dir / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("old")
    tree = {"bin/node": "cba06b5736faf67e54b07b561eae94395e774c517a7d910a54369e1263ccfbd4"}
    marker = {
        "format_version": 1,
        "bundle_id": old,
        "tree": tree,
        "package_lock_sha256": "",
        "srt_capability": "not_applicable",
    }
    import json

    (old_dir / ".pico-toolchain.json").write_text(json.dumps(marker))
    (old_dir / ".pico-toolchain.json").chmod(0o600)
    node.chmod(0o500)
    old_dir.chmod(0o700)
    (root / "bundles" / "unknown").mkdir()
    root.chmod(0o700)
    retirement_metadata = retirement()
    toolchain = SandboxToolchain(
        root,
        manifest={
            "platforms": {"linux-x64": {"identity": current, "tree": {}}},
            "retirements": {
                old: {
                    "platform": "linux",
                    "arch": "x64",
                    "tree_sha256": bundle_tree_hash(tree),
                    **retirement_metadata,
                }
            },
        },
        platform="linux-x64",
    )

    inventory = toolchain.inventory()

    assert inventory[0]["verified"] is True
    assert inventory[0]["retirement"] == retirement_metadata
    assert inventory[1]["verified"] is False
