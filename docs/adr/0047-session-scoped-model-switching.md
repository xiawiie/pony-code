# ADR-0047: Session-scoped model switching

## Status

Accepted for Pony 1.0 before its first release tag.

## Context

Pony previously treated `protocol_family`, `model`, and `endpoint_hash` as one immutable Session binding. This prevented unsafe
cross-protocol replay, but also forced a new Session when a user only wanted to move from one model to another model served by the
same endpoint and wire protocol. A named target registry, Provider picker, or second configuration file would solve a much larger
problem than this interaction requires and would duplicate the four-variable `.env` contract.

Some Provider responses contain opaque continuation state tied to the model that created it. OpenAI Responses reasoning and
Anthropic thinking/redacted-thinking blocks may be carried in `_pony_provider_state`; Pony cannot prove that this state is valid for
another model. Model selection also affects context budgets, token accounting, and the client factory used by named delegates, so
replacing only the displayed model string would leave the runtime inconsistent.

## Decision

- `.env` remains the only persistent Provider default. `PONY_MODEL` selects the model for a fresh Session unless `run` or `repl`
  receives `--model MODEL`; the CLI override never edits `.env`.
- `/model` displays the active Provider family, protocol, and model without writing Session state. `/model MODEL` requests a model
  change between top-level turns and uses the same handler in the TUI and plain REPL.
- A model change is allowed only when a client built by the existing Transport factory reports the same `protocol_family` and
  `endpoint_hash` as the active binding. Pony does not add a model catalog, picker, Provider registry, network discovery, fallback,
  or cross-endpoint credential selection.
- The Session model is authoritative on resume. The repository `.env` must still resolve to a compatible Provider protocol and
  endpoint, but its model value does not overwrite the saved Session model. An explicit resume-time `--model` is applied only after
  that compatibility check succeeds.
- `SessionStore.set_provider_model()` validates both bindings under the Session lock, compares the expected active binding and
  exact leaf captured before client construction, permits only the model field to differ, and appends one `session_info` entry.
  Concurrent Session change, protocol drift, or endpoint drift returns `model_session_mismatch` without writing.
- Runtime replacement is prepared before the Session write. After the atomic write succeeds, Pony installs the new client, model
  capabilities, context/output budget, token counter, and delegate client factory, then reloads the active Session projection.
- A Session containing `_pony_provider_state` cannot change models. It returns `model_session_mismatch` without writing rather than
  replaying model-specific opaque state. Canonical provider-neutral messages remain the only transcript.
- Model names must be non-empty, at most 200 characters, contain no surrounding whitespace or line breaks, and pass the active
  secret redactor unchanged. Invalid values are rejected before persistence.

## Consequences

- Users can use Claude-style `/model` and one-process `--model` selection without introducing profiles or changing Provider
  configuration.
- The active model becomes Session state and survives resume, fork, rewind, and clone through the existing binding projection.
- Switching does not test whether the endpoint actually serves the requested model. The next normal request reports the Provider
  error; Pony never switches protocol or retries the user task through another model.
- Sessions with opaque Provider continuation state must remain on their bound model or start a new Session.
- Cross-Provider, cross-protocol, cross-endpoint, model discovery, and account-level model management remain out of scope.
