# AI Runtime

Routing a task to a model, running it under budget, and evaluating the result.

## Language

**TaskPort**:
A fixed, application-code declaration of one task type's prompt, allowed tools, and output shape. The safety
gate for tool eligibility lives here — never in the database, and never decided at prompt-render time.

**ModelDefinition**:
An approved model/provider entry in the platform's catalog. Global, not scoped to any one workspace.

**RoutingPolicy**:
A versioned statement of which models a task type may consider, plus its budget constraints.
_Avoid_: Policy alone when [Automation](../automation/CONTEXT.md)'s AutomationPolicy is also in scope — they
share only the English word.

**RunBudget**:
The derived, in-memory budget for one run, built from a RoutingPolicy. Not itself a persisted record.

**PromptVersion** / **ToolDefinition**:
An immutable-once-active, versioned prompt template or tool contract. Never edited — only activated by
publishing a new version.

**AiRun** / **AiRunStep**:
The persisted result of one orchestration loop, and its individual model/tool steps.

**EvaluationSet** / **EvaluationRun**:
A global, labelled dataset for one task type, and one scored run of a model against it — gates whether a
prompt or model version can be promoted.

**CircuitBreaker**:
A per-model, in-memory reliability breaker feeding routing eligibility.
