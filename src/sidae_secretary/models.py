from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Event:
    external_id: str
    source: str
    start_at: str
    end_at: str
    title: str
    location: str | None = None
    rrule: str | None = None
    metadata_json: str = "{}"
    user_id: int | None = None


@dataclass
class Task:
    external_id: str
    source: str
    due_at: str | None
    title: str
    status: str
    metadata_json: str = "{}"
    user_id: int | None = None


@dataclass
class Artifact:
    external_id: str
    source: str
    filename: str
    icloud_path: str | None
    content_hash: str | None
    metadata_json: str = "{}"
    updated_at: str | None = None
    user_id: int | None = None


@dataclass
class Notification:
    external_id: str
    source: str
    created_at: str
    title: str
    body: str | None
    url: str | None
    metadata_json: str = "{}"
    user_id: int | None = None


@dataclass
class SyncState:
    job_name: str
    last_run_at: str | None
    last_cursor_json: str | None


@dataclass
class InboxItem:
    external_id: str
    source: str
    received_at: str
    title: str
    body: str | None
    item_type: str
    draft_json: str = "{}"
    processed: bool = False
    metadata_json: str = "{}"
    id: int | None = None
    user_id: int | None = None


@dataclass
class Summary:
    external_id: str
    source: str
    created_at: str
    title: str
    body: str
    action_item: str | None
    metadata_json: str = "{}"
    user_id: int | None = None


@dataclass
class Course:
    canonical_course_id: str
    source: str
    external_course_id: str | None
    display_name: str
    metadata_json: str = "{}"
    user_id: int | None = None


@dataclass
class CourseAlias:
    canonical_course_id: str
    alias: str
    normalized_alias: str
    alias_type: str
    source: str
    metadata_json: str = "{}"
    id: int | None = None
    user_id: int | None = None
