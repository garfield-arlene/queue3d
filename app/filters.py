"""Shared query-filtering helpers for every job/log/user listing page in
this app. Per the user: filters should be available everywhere the
underlying data exists (substring, color, status, est. print time, and
date/time range on every job table; a submitter filter on the admin-side
ones; an action filter on the activity log) - built once here rather than
a slightly different version in each of the five-plus routes that need
some subset of them.

Every filter is a plain GET query param, never a POST - a filtered
view's URL is always bookmarkable/shareable and works with zero
JavaScript, matching how this app already prefers server-rendered pages
over client-side state (see e.g. the plain <form method=post> uploads).
"""

from datetime import date, datetime, time, timedelta
from datetime import timezone as _utc

import templates_env
from models import Job, JobEvent, User


def local_date_bounds(date_from: str | None, date_to: str | None) -> tuple[datetime | None, datetime | None]:
    """Two 'YYYY-MM-DD' strings (from plain HTML `<input type="date">`
    fields) -> the UTC instants bounding that whole range of *local*
    calendar days, in the admin's configured display timezone (see
    templates_env.local_time - this is the exact inverse operation).
    Deliberately not literal UTC midnight, which would silently shift
    the filtered range by however many hours the display timezone is
    offset from UTC - a date typed as "today" should mean today in
    whatever zone the rest of the app already displays timestamps in,
    not in UTC specifically. `date_to` is inclusive of the entire day
    (bounded by the start of the *next* local day), matching what
    someone picking an end date actually means - "through the end of
    that day," not "up to its literal midnight." Either side is `None`
    if blank or unparseable - "no bound on that side," not an error;
    a filter form is expected to be used partially."""
    tz = templates_env.get_display_timezone()

    def parse(s: str | None) -> date | None:
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None

    start = None
    d_from = parse(date_from)
    if d_from is not None:
        start = datetime.combine(d_from, time.min, tzinfo=tz).astimezone(_utc.utc).replace(tzinfo=None)

    end = None
    d_to = parse(date_to)
    if d_to is not None:
        end = (datetime.combine(d_to, time.min, tzinfo=tz) + timedelta(days=1)).astimezone(_utc.utc).replace(tzinfo=None)

    return start, end


# Sentinel for "Any available" in a color filter - a real Color.name value
# can never equal this (colors are admin-typed strings, and this isn't a
# realistic one), so it's safe to use as "job.color_name IS NULL" without
# a second, separately-named query param just for that one case.
ANY_COLOR = "__any__"


def apply_job_filters(
    query,
    *,
    q: str | None = None,
    color: str | None = None,
    status: str | None = None,
    min_minutes: float | None = None,
    max_minutes: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_column=None,
    user: str | None = None,
):
    """Applies whichever of these filters were actually given to a
    `select(Job)`-based query (or one still selecting Job's own columns
    alongside something else) - shared by the user's own dashboard and
    every admin job-listing view, differing only in which timestamp
    column `date_column` should mean "date" for that particular view
    (e.g. `Job.queued_at` for the live queue, `Job.finished_at` for
    finished jobs - whichever one that view already shows as its own
    "date" column) and whether `user` (a submitter-name substring) is
    even offered at all - admin views only, since a user's own dashboard
    is already scoped to just them and has no submitter column to filter
    on in the first place.

    `min_minutes`/`max_minutes` compare against `Job.duration_estimate_s`
    (stored in seconds) converted to minutes, matching what every
    duration this app displays is already rendered in (see
    jobs.format_duration) - a filter typed in minutes should mean
    minutes, not raw seconds nobody actually thinks in."""
    if q:
        query = query.where(Job.original_filename.ilike(f"%{q}%"))
    if color:
        if color == ANY_COLOR:
            query = query.where(Job.color_name.is_(None))
        else:
            query = query.where(Job.color_name == color)
    if status:
        query = query.where(Job.status == status)
    if min_minutes is not None:
        query = query.where(Job.duration_estimate_s >= min_minutes * 60)
    if max_minutes is not None:
        query = query.where(Job.duration_estimate_s <= max_minutes * 60)
    if date_column is not None:
        start, end = local_date_bounds(date_from, date_to)
        if start is not None:
            query = query.where(date_column >= start)
        if end is not None:
            query = query.where(date_column < end)
    if user:
        query = query.join(User, Job.user_id == User.id).where(User.name.ilike(f"%{user}%"))
    return query


# The full set of query-param names job_filter_params below binds - used
# by routers/admin.py's _perform_action to pull the same fields back out
# of a plain POST's request.query_params (no FastAPI dependency injection
# available there, unlike the GET pages this dependency normally serves),
# so a queue action taken from a filtered view can carry that filter
# forward to wherever it redirects/re-renders next.
JOB_FILTER_KEYS = ("q", "color", "status", "min_minutes", "max_minutes", "date_from", "date_to", "user")


def _parse_float(s: str | None) -> float | None:
    """"" (a blank <input type=number> - what a browser actually submits
    for a filter field nobody touched, since a GET form sends every one
    of its fields regardless of whether it has a value) and None both
    mean "no bound," same as anything else unparseable - not an error. A
    real bug caught before it shipped: min_minutes/max_minutes were
    originally typed as `float | None` directly on job_filter_params'
    own signature, letting FastAPI's own query-param coercion reject ""
    with a 422 - meaning *every* normal use of the filter form (leaving
    either field blank, the overwhelmingly common case) failed outright,
    confirmed live with a real request before this fix. Kept as a
    module-level function, not inlined twice, since both
    job_filter_params and job_filters_from_query_params below need the
    exact same parsing."""
    try:
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def job_filters_from_query_params(query_params) -> dict:
    """Same result as job_filter_params below, but reading from a plain
    Starlette QueryParams (request.query_params) instead of letting
    FastAPI bind/coerce them - for a POST route with no dependency
    injection of its own to do that job."""
    raw = {k: query_params.get(k) for k in JOB_FILTER_KEYS}
    raw["min_minutes"] = _parse_float(raw["min_minutes"])
    raw["max_minutes"] = _parse_float(raw["max_minutes"])
    return raw


def job_filter_params(
    q: str | None = None,
    color: str | None = None,
    status: str | None = None,
    min_minutes: str | None = None,
    max_minutes: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user: str | None = None,
) -> dict:
    """A plain function, used as a FastAPI dependency (`Depends(job_filter_params)`)
    on every job-listing route - user.py's own dashboard and every
    admin job-listing view alike - so the full set of query params a
    filter form (see templates/_job_filters.html) can ever submit is
    bound and validated in exactly one place, not retyped into every
    route's own signature. Returns a plain dict rather than a dataclass
    so a route can pass it straight through as **filters into
    apply_job_filters/jobs_for_user/active_jobs/finished_jobs (all of
    which already default every one of these to None) with no
    translation step. `user` is accepted here even on routes that never
    expose the field in their own template (see _job_filters.html's
    filter_show_user) - always None there since nothing ever submits it,
    same as any other filter nobody's actually using yet.

    min_minutes/max_minutes are typed as `str | None` here, not `float`,
    deliberately - see _parse_float's own docstring for the real 422
    this avoids on every ordinary (mostly-blank) filter form
    submission."""
    return {
        "q": q,
        "color": color,
        "status": status,
        "min_minutes": _parse_float(min_minutes),
        "max_minutes": _parse_float(max_minutes),
        "date_from": date_from,
        "date_to": date_to,
        "user": user,
    }


def event_filter_params(
    q: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Same idea as job_filter_params above, for the admin activity log
    (see apply_event_filters) - the one filterable page whose fields
    don't line up with a Job listing at all."""
    return {
        "q": q,
        "actor": actor,
        "action": action,
        "date_from": date_from,
        "date_to": date_to,
    }


def user_filter_params(
    q: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Same idea as job_filter_params above, for the admin users list -
    its own small, distinct field set (User, not Job/JobEvent): a
    name substring, active/disabled, and a date range on created_at.
    Not routed through apply_job_filters at all - a different model
    entirely, and simple enough not to need its own apply_*_filters
    function the way jobs/events do (see apply_user_filters below,
    which is a plain in-Python filter, not a SQL WHERE builder - see its
    own docstring for why)."""
    return {"q": q, "status": status, "date_from": date_from, "date_to": date_to}


def apply_user_filters(users: list[User], *, q=None, status=None, date_from=None, date_to=None) -> list[User]:
    """Filters an already-fetched list[User] in Python, not a SQL query
    like apply_job_filters/apply_event_filters above - deliberately: the
    users table is realistically tiny for a single-printer, single-school
    deployment (unlike Job/JobEvent, which is exactly why those two get
    real indexes - see models.py/db.py's schema 6.3.0), so there's no
    performance reason to push this into SQL, and doing it here avoids
    needing a second, SQL-specific version of the same three checks."""
    start, end = local_date_bounds(date_from, date_to)
    result = []
    for user in users:
        if q and q.lower() not in user.name.lower():
            continue
        if status == "active" and user.disabled:
            continue
        if status == "disabled" and not user.disabled:
            continue
        created = user.created_at.replace(tzinfo=None) if user.created_at.tzinfo else user.created_at
        if start is not None and created < start:
            continue
        if end is not None and created >= end:
            continue
        result.append(user)
    return result


def apply_event_filters(
    query,
    *,
    q: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Same idea as apply_job_filters above, for the admin activity log
    (models.JobEvent) instead of a job listing - the one filterable table
    that isn't Job rows at all. `query` is expected to already be joined
    to Job (see jobs.all_events, which joins for the filename/photo_path
    every row shows) so `q` can match against either side: a job's own
    filename, or a plain-text account-lifecycle detail (job_id is None
    for those - see JobEvent's own docstring - so there's no filename to
    match at all, and detail is the only thing worth a substring search
    on). `actor` is a substring match (e.g. "user:" or "admin:" alone
    narrows to every action by either role at once, not just one specific
    account), `action` an exact match (see jobs.distinct_event_actions -
    the log page offers this as a dropdown of real recorded actions, not
    a freeform field, so exact match is correct here unlike the substring
    filters everywhere else in this module)."""
    if q:
        query = query.where(
            Job.original_filename.ilike(f"%{q}%") | JobEvent.detail.ilike(f"%{q}%")
        )
    if actor:
        query = query.where(JobEvent.actor.ilike(f"%{actor}%"))
    if action:
        query = query.where(JobEvent.action == action)
    start, end = local_date_bounds(date_from, date_to)
    if start is not None:
        query = query.where(JobEvent.at >= start)
    if end is not None:
        query = query.where(JobEvent.at < end)
    return query
