# Planning

Capturing and tracking discrete units of work.

## Language

**Task**:
A discrete unit of work with an owner, a status, a manual priority, and an optional due date. Its lifecycle is
captured, planned, in_progress, blocked, then completed, cancelled, or archived.

## Open question

`docs/domain/DOMAIN-MODEL.md` describes Planning as also owning Goal, Project, CalendarEvent, Meeting, and
Reminder. Verified against code: Project and Reminder don't exist anywhere; Goal exists but is a personal
habit-tracking construct with no relation to Task (see [Personal](../personal/CONTEXT.md)); CalendarEvent and
Meeting are owned by separate real contexts ([Calendar](../calendar/CONTEXT.md),
[Scheduling](../scheduling/CONTEXT.md)). Task is the only concept this context actually owns today. Whether
Project/Reminder are still-intended future work or should be dropped from the canonical model is an open
product decision, not something this glossary resolves.
