from pathlib import Path

path = Path("backend/ecc/domains/scheduling/meetings.py")
text = path.read_text()
needle = """        if payload.calendar_event_id is not None:
            event = _calendar_event(session, auth, payload.calendar_event_id)
            if event is None or event[\"archived_at\"] is not None:
                raise HTTPException(status_code=404, detail=\"CALENDAR_EVENT_NOT_FOUND\")
"""
replacement = """        if payload.calendar_event_id is not None:
            link_key = f\"{auth.workspace_id}:calendar-meeting:{payload.calendar_event_id}\"
            session.execute(
                text(\"SELECT pg_advisory_xact_lock(hashtextextended(:link_key, 0))\"),
                {\"link_key\": link_key},
            )
            event = _calendar_event(session, auth, payload.calendar_event_id)
            if event is None or event[\"archived_at\"] is not None:
                raise HTTPException(status_code=404, detail=\"CALENDAR_EVENT_NOT_FOUND\")
            existing = session.execute(
                text(
                    \"\"\"
                    SELECT id FROM meetings
                    WHERE workspace_id = :workspace_id
                      AND calendar_event_id = :calendar_event_id
                    \"\"\"
                ),
                {
                    \"workspace_id\": auth.workspace_id,
                    \"calendar_event_id\": payload.calendar_event_id,
                },
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail=\"CALENDAR_EVENT_ALREADY_LINKED\",
                )
"""
if needle in text:
    text = text.replace(needle, replacement, 1)
path.write_text(text)
