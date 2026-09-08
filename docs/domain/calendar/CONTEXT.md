---
id: CONTEXT-CALENDAR
title: Calendar Context
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Calendar

Scheduled time intervals.

## Language

**CalendarEvent**:
A scheduled interval with a start, an end, a timezone, and a confirmed/tentative/cancelled status. May
originate from an external source or be created locally.

## Relationship to Scheduling

A [Meeting](../scheduling/CONTEXT.md) may optionally link to one CalendarEvent. Meeting and CalendarEvent are
independently persisted, independently lifecycled entities in two separate contexts — a Meeting only borrows
its timing from a linked CalendarEvent, it does not become one.
