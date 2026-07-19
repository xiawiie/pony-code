import pytest

from pony.tools.permissions import (
    PermissionDecision,
    PermissionMode,
    decide_permission,
    validate_permission_mode,
)


def test_permission_modes_have_stable_values():
    assert {mode.value for mode in PermissionMode} == {
        "default",
        "acceptEdits",
        "auto",
        "bypassPermissions",
        "plan",
        "dontAsk",
    }


@pytest.mark.parametrize("mode", PermissionMode)
def test_untrusted_project_and_explicit_deny_fail_closed(mode):
    assert (
        decide_permission(
            project_trusted=False,
            mode=mode,
            effect_class="read_only",
            explicit="allow",
        )
        is PermissionDecision.DENY
    )
    assert (
        decide_permission(
            project_trusted=True,
            mode=mode,
            effect_class="read_only",
            explicit="deny",
        )
        is PermissionDecision.DENY
    )


def test_plan_forces_mutation_to_ask_even_when_explicitly_allowed():
    assert (
        decide_permission(
            project_trusted=True,
            mode="plan",
            effect_class="workspace_write",
            explicit="allow",
            builtin_edit=True,
        )
        is PermissionDecision.ASK
    )


def test_accept_edits_only_auto_allows_builtin_file_edits():
    def decide(effect, builtin=False):
        return decide_permission(
            project_trusted=True,
            mode="acceptEdits",
            effect_class=effect,
            builtin_edit=builtin,
        )

    assert decide("workspace_write", True) is PermissionDecision.ALLOW
    assert decide("workspace_write") is PermissionDecision.ASK
    assert decide("memory_write", True) is PermissionDecision.ASK


def test_auto_only_allows_explicitly_classified_low_risk_mutation():
    def decide(auto_allow):
        return decide_permission(
            project_trusted=True,
            mode="auto",
            effect_class="workspace_write",
            auto_allow=auto_allow,
        )

    assert decide(True) is PermissionDecision.ALLOW
    assert decide(False) is PermissionDecision.DENY


def test_bypass_skips_prompts_but_not_trust_or_explicit_deny():
    assert (
        decide_permission(
            project_trusted=True,
            mode="bypassPermissions",
            effect_class="workspace_write",
        )
        is PermissionDecision.ALLOW
    )
    assert (
        decide_permission(
            project_trusted=False,
            mode="bypassPermissions",
            effect_class="workspace_write",
        )
        is PermissionDecision.DENY
    )


def test_manual_is_public_alias_for_internal_default():
    assert validate_permission_mode("manual") == "default"


def test_dont_ask_denies_unapproved_mutation_and_honors_explicit_allow():
    implicit = decide_permission(
        project_trusted=True,
        mode="dontAsk",
        effect_class="workspace_write",
    )
    allowed = decide_permission(
        project_trusted=True,
        mode="dontAsk",
        effect_class="workspace_write",
        explicit="allow",
    )
    prompted = decide_permission(
        project_trusted=True,
        mode="dontAsk",
        effect_class="workspace_write",
        explicit="ask",
    )

    assert implicit is PermissionDecision.DENY
    assert allowed is PermissionDecision.ALLOW
    assert prompted is PermissionDecision.DENY


def test_read_only_defaults_to_allow_and_unknown_input_denies():
    assert (
        decide_permission(
            project_trusted=True,
            mode="default",
            effect_class="read_only",
        )
        is PermissionDecision.ALLOW
    )
    assert (
        decide_permission(
            project_trusted=True,
            mode="default",
            effect_class="unknown",
        )
        is PermissionDecision.DENY
    )


@pytest.mark.parametrize("noncanonical", ("accept_edits", "dont_ask", "manual"))
def test_internal_decision_rejects_noncanonical_modes(noncanonical):
    assert (
        decide_permission(
            project_trusted=True,
            mode=noncanonical,
            effect_class="read_only",
        )
        is PermissionDecision.DENY
    )
