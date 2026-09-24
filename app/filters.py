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
from models import Job, User


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
