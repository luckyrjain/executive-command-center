# Automation

Durable, resumable execution of multi-step workflows, with human approval where required.

## Language

**WorkflowVersion**:
An immutable, versioned workflow graph — the same versioning shape as [AI Runtime](../ai-runtime/CONTEXT.md)'s
PromptVersion/ToolDefinition.

**AutomationPolicy**:
The scope, value and count limits, and expiry governing a workflow's authorized execution envelope.
_Avoid_: Policy alone when [AI Runtime](../ai-runtime/CONTEXT.md)'s RoutingPolicy is also in scope — one
governs model eligibility, this one governs execution authority; they share only the English word.

**WorkflowRun**:
The crash-safe, lease-based execution record for one run of a WorkflowVersion.

**ApprovalRequest**:
A pending human decision gating a high-impact step before it may proceed.

**KillSwitch**:
A global or per-workflow mechanism that stops new and in-flight runs.

**ActionAdapter**:
One write action a workflow step can perform (for example, adding a comment to an external system) —
declares whether it's reversible and what high-impact category it falls under.
_Avoid_: ConnectorAdapter (see [Engineering](../engineering/CONTEXT.md)) — a different concept, already
correctly disambiguated in code: an ActionAdapter is one write action; a ConnectorAdapter is one provider's
whole account lifecycle. "Adapter" alone is used for at least three unrelated things across this codebase
(this one, ConnectorAdapter, and AI Runtime's OllamaAdapter) — always qualify it.
