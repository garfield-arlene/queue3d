"""A free-text support/bug-report channel, from either account type - see
models.Feedback for the full design rationale. Shared by both
routers/user.py's own /feedback and routers/admin.py's /admin/feedback
(each has its own GET/POST route - see routers/help.py's own docstring for
why this app never does role-detection on one shared route) rather than
living in either one, the same reasoning jobs.py's own shared functions
already follow.
"""

from datetime import datetime
from datetime import timezone as _utc

from sqlmodel import Session, select

import templates_env
from jobs import log_event
from models import Feedback, Job


def parse_occurred_at(s: str | None) -> datetime | None:
    """A plain HTML `<input type="datetime-local">` value
    ("YYYY-MM-DDTHH:MM") -> the UTC instant it means in the admin's
    configured display timezone - the same conversion
    filters.local_date_bounds already does for a bare date, just with an
    explicit time component instead of defaulting to midnight. None if
    blank/unparseable - "approximately when did this happen" is an
    optional prompt, not a required field. Returns an aware UTC datetime
    - sqlmodel>=0.0.45 rejects a naive datetime written to its now
    tz-aware Feedback.occurred_at column."""
    if not s:
        return None
    try:
        naive = datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    tz = templates_env.get_display_timezone()
    return naive.replace(tzinfo=tz).astimezone(_utc.utc)


def create_feedback(
    session: Session,
    *,
    actor: str,
    description: str,
    job: Job | None,
    occurred_at_raw: str | None,
) -> Feedback:
    """Builds and persists one Feedback row, plus a matching activity-log
    entry (per the user, elsewhere: "all actions should be captured" -
    the same reasoning every other account/job action already logs one,
    see jobs.log_event). `job` is already resolved and access-checked by
    the caller (a user may only reference their own job; an admin, any
    job - see routers/user.py's submit_feedback / routers/admin.py's own
    copy) - this just snapshots its filename alongside the FK, the same
    way Job.reviewed_by_name snapshots a reviewing admin's own username,
    so a reference here stays readable even if the job it points to is
    ever gone."""
    fb = Feedback(
        actor=actor,
        description=description,
        job_id=job.id if job else None,
        job_filename=job.original_filename if job else None,
        occurred_at=parse_occurred_at(occurred_at_raw),
    )
    session.add(fb)
    session.commit()
    session.refresh(fb)

    log_event(
        session,
        job.id if job else None,
        actor,
        "feedback_submitted",
        detail=description if len(description) <= 200 else description[:197] + "...",
    )
    session.commit()
    return fb


def list_feedback(session: Session, *, actor: str | None = None) -> list[Feedback]:
    """Every submitted Feedback row, most recent first - all of them for
    an admin's own review page, or just one account's own past
    submissions (actor=f"user:{name}") for that account's own "my
    feedback" history. Not filtered/paginated beyond that (see
    support_bundle.build_support_bundle for the same query, unfiltered,
    feeding the downloadable bundle instead) - this table is realistically
    small for a single-school deployment, the same reasoning
    filters.apply_user_filters already gives for not pushing the (much
    smaller still) users table through SQL-level filtering either."""
    query = select(Feedback).order_by(Feedback.submitted_at.desc())
    if actor is not None:
        query = query.where(Feedback.actor == actor)
    return session.exec(query).all()
