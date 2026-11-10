"""Schedules: jobs as data.

One file per schedule, `var/settings/schedules/<name>.toml`, copied once from
config/schedules at the Scheduler's first start (settings/seed.py) and read only from
there:

    label = "Radar"
    summary = "Reads the news."  # one sentence for the onboarding's list ("": none)
    cron = "0 2 * * *"            # minute hour day month weekday, the app's time zone
    after = ["tidy"]              # the runs of the same night this one follows
    reviewer = "mentor.md"        # a prompt beside it it talks with first ("": none)
    turns = 8                     # the reviewer's lines at most
    note = "radar/{{date}}.md"    # where its answer is written ("": nowhere)
    role = "chat"                 # the model role its turns run on
    on = true
    idle_only = false             # true: starts only when nobody used the app for 30 min,
                                  # else that run is skipped (never caught up)
    wall_minutes = 10             # left out: the Routine time limit (Settings, Advanced)
    on_fail = "retry"             # when a run fails: "retry" runs it again up to three
                                  # times for the same night, "skip" ends it skipped with no notice
    daytime = false               # true: its dates are the calendar date of its slot
    prompt = '''What happened in the world on {{day_before}}? ...'''

The prompt, the reviewer prompt and the note path are filled from the run's date, never
from the clock (`fill`): {{date}} the night's date (night_of: every day-based name uses
it), or for a `daytime` job the calendar date of its slot (`run_date`), {{day_before}}, {{weekday}}, {{result:<schedule>}} that schedule's answer of the
same night, its name, its pronouns and {{username}}. A
prompt may also hold what schedules/variables.py fills when the run starts: {{mood}},
{{on_mind}}, {{tool_failures}} and {{voice_counts}}. Runs: schedules/runs.py.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REVIEWER = re.compile(r"^[a-z][a-z0-9_-]{0,40}\.md$")   # room for <name>-reviewer.md
KEYS = {"label", "summary", "cron", "prompt", "after", "reviewer", "turns", "note", "role",
        "on", "idle_only", "wall_minutes", "daytime", "on_fail"}
ON_FAIL = ("retry", "skip")   # When a run fails (schedules/runs.py _fail)
RESULT = re.compile(r"\{\{result:([a-z][a-z0-9_-]{0,31})\}\}")
DAILY = re.compile(r"^(\d{1,2}) (\d{1,2}) \* \* \*$")
VARIABLES = ("date", "day_before", "weekday", "username", "name", "mood", "on_mind",
             "tool_failures", "voice_counts")
NONE = "(none)"
PROMPT_MAX = 50_000          # characters


@dataclass(frozen=True)
class Schedule:
    name: str
    label: str
    cron: str
    prompt: str
    after: tuple[str, ...] = ()
    reviewer: str = ""
    reviewer_prompt: str = ""
    turns: int = 8
    note: str = ""
    role: str = "chat"
    on: bool = True
    idle_only: bool = False
    wall_minutes: int | None = None
    daytime: bool = False
    summary: str = ""
    on_fail: str = "retry"
    title: str = ""            # the label with its name and the user's filled in (load)

    @property
    def time(self) -> str | None:
        """"02:00" for a daily schedule, else None."""
        m = DAILY.match(self.cron)
        return f"{int(m[2]):02d}:{int(m[1]):02d}" if m else None

    def raw(self) -> dict:
        """What its file holds."""
        return {"label": self.label, "summary": self.summary, "cron": self.cron,
                "after": list(self.after),
                "reviewer": self.reviewer, "turns": self.turns, "note": self.note,
                "role": self.role, "on": self.on,
                "idle_only": self.idle_only,
                "daytime": self.daytime,
                "on_fail": self.on_fail,
                **({"wall_minutes": self.wall_minutes} if self.wall_minutes else {}),
                "prompt": self.prompt}


DAYS = ("Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays",
        "Saturdays", "Sundays")        # cron weekdays 0 to 7


SAID = 6                       # the most times one schedule says as a list


def _hours(field: str) -> list[int] | None:
    """The hours a cron hour field names, in order; None for a shape not read here."""
    out: set[int] = set()
    for part in field.split(","):
        span, _, step = part.partition("/")
        if not (step or "1").isdigit() or int(step or "1") < 1:
            return None
        # a start with a step runs to the day's end: 3/5 is 3-23/5
        lo, _, hi = ("0-23" if span == "*" else
                     f"{span}-23" if step and span.isdigit() else span).partition("-")
        if not lo.isdigit() or not (hi or lo).isdigit():
            return None
        out.update(range(int(lo), int(hi or lo) + 1, int(step or "1")))
    return sorted(out) if out and max(out) <= 23 else None


def _days(field: str) -> str | None:
    """"" for every day, "Sundays", "Mondays and Thursdays", "Weekdays", "Weekends";
    None for a shape not read here."""
    if field == "*":
        return ""
    if field == "1-5":
        return "Weekdays"
    if not all(d.isdigit() and int(d) <= 7 for d in field.split(",")):
        return None
    days = list(dict.fromkeys(DAYS[int(d)] for d in field.split(",")))
    if sorted(days) == ["Saturdays", "Sundays"]:
        return "Weekends"
    return _and(days)


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def words(cron: str) -> str:
    """A cron line in plain words, 24h times: "Daily at 04:00", "Daily at 09:00 and
    17:00", "Every 2 hours from 10:00 to 22:00" (a step that divides 24), "Sundays at 03:00". A
    day of the month, a month, a minute that is not one number or more than SAID times
    that are no steady interval: the cron line itself."""
    parts = cron.split()
    if len(parts) != 5 or not parts[0].isdigit() or parts[2:4] != ["*", "*"]:
        return cron
    minute, hour = int(parts[0]), parts[1]
    hours, days = _hours(hour), _days(parts[4])
    if hours is None or days is None or minute > 59:
        return cron
    at = [f"{h:02d}:{minute:02d}" for h in hours]
    gaps = {b - a for a, b in zip(hours, hours[1:])}
    # a steady interval only for a step that divides the day: */5 runs at 20:00, then 00:00
    if len(hours) > 2 and len(gaps) == 1 and 24 % next(iter(gaps)) == 0:
        n = gaps.pop()
        every = "Every hour" if n == 1 else f"Every {n} hours"
        if not (hour.startswith("*") and minute == 0):
            every += f" from {at[0]} to {at[-1]}"
        return f"{days}, {every[0].lower()}{every[1:]}" if days else every
    if len(at) > SAID:
        return cron
    return f"{days or 'Daily'} at {_and(at)}"


def shown(s: Schedule) -> str:
    """A schedule's name as people read it: its filled label."""
    return s.title or s.label


def run_roles() -> list[str]:
    """The roles a schedule can run on: every chat role but the writer (a tool's,
    skills/writer.py) and Bastet's own (guard/small_role.py)."""
    from ..settings.registry import ROLES
    return [r for r, v in ROLES.items() if "chat" in v["caps"] and r not in ("writer", "guard")]


def _invalid(field: str, sentence: str):
    from ..settings.files import Invalid
    return Invalid(field, sentence)


def parse(name: str, raw: dict, reviewer_prompt: str | None) -> Schedule:
    """One schedule from its values and its reviewer prompt (None: the file is missing).
    Raises Invalid with the field and one sentence."""
    from croniter import croniter
    if not NAME.match(name):
        raise _invalid("name", "A name is lowercase letters, digits, '-' or '_', starting "
                               "with a letter.")
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        raise _invalid(unknown[0], f"'{unknown[0]}' is not a schedule setting.")
    label, cron, prompt = raw.get("label"), raw.get("cron"), raw.get("prompt")
    if not isinstance(label, str) or not label.strip() or len(label) > 60:
        raise _invalid("label", "The name is 1 to 60 characters.")
    summary = raw.get("summary", "")
    if not isinstance(summary, str) or len(summary) > 120:
        raise _invalid("summary", "The summary is one sentence of at most 120 characters.")
    if not isinstance(cron, str) or not croniter.is_valid(cron.strip()):
        raise _invalid("cron", "The time is a cron line, such as 0 2 * * * for every day "
                               "at 02:00.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_MAX:
        raise _invalid("prompt", f"The prompt is text of 1 to {PROMPT_MAX:,} characters.")
    after = raw.get("after", [])
    if not isinstance(after, list) or not all(isinstance(a, str) and NAME.match(a)
                                              for a in after):
        raise _invalid("after", "Runs after is a list of schedule names.")
    if name in after:
        raise _invalid("after", "A schedule cannot run after itself.")
    reviewer = raw.get("reviewer", "")
    if not isinstance(reviewer, str) or (reviewer and not REVIEWER.match(reviewer)):
        raise _invalid("reviewer", "The reviewer is a prompt file name such as mentor.md.")
    if reviewer and reviewer_prompt is None:
        raise _invalid("reviewer", f"The reviewer prompt {reviewer} is missing.")
    if reviewer and not reviewer_prompt.strip():
        raise _invalid("reviewer_prompt", "The reviewer prompt is empty.")
    turns = raw.get("turns", 8)
    if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
        raise _invalid("turns", "Turns is a whole number of 1 or more.")
    note = raw.get("note", "")
    if not isinstance(note, str) or (note and (
            not note.endswith(".md") or note.startswith("/") or ".." in note.split("/"))):
        raise _invalid("note", "The note is a notebook path ending in .md, such as "
                               "radar/{{date}}.md.")
    role = raw.get("role", "chat")
    if role not in run_roles():
        from ..settings.registry import ROLES
        raise _invalid("role", "The role is one of "
                               f"{', '.join(ROLES[r]['label'] for r in run_roles())}.")
    flags = {}
    for key, default in (("on", True), ("idle_only", False), ("daytime", False)):
        flags[key] = raw.get(key, default)
        if not isinstance(flags[key], bool):
            raise _invalid(key, "This must be true or false.")
    wall = raw.get("wall_minutes")
    if wall is not None and (not isinstance(wall, int) or isinstance(wall, bool)
                             or wall < 1):
        raise _invalid("wall_minutes", "The time limit is a whole number of minutes, "
                                       "or empty.")
    on_fail = raw.get("on_fail", "retry")
    if on_fail not in ON_FAIL:
        raise _invalid("on_fail", "When a run fails is Retry or Skip.")
    return Schedule(name, label.strip(), " ".join(cron.split()), prompt.strip(),
                    tuple(dict.fromkeys(after)), reviewer,
                    (reviewer_prompt or "").strip() if reviewer else "", turns,
                    note.strip(), role, flags["on"], flags["idle_only"], wall,
                    flags["daytime"], summary.strip(), on_fail)


def check_after(schedules: dict[str, Schedule], one: Schedule) -> None:
    """`one`'s follows name schedules that exist and make no circle. Raises Invalid."""
    for a in one.after:
        if a not in schedules:
            raise _invalid("after", f"There is no schedule {a}.")
    every = {**schedules, one.name: one}
    seen: set[str] = set()

    def walk(n: str, path: tuple[str, ...]) -> None:
        if n in path:      # the circle closes at `one`: path[1] is what it would follow
            raise _invalid("after", f"{shown(every[path[1]])} already runs after "
                                    f"{shown(one)}." if len(path) > 1 else
                                    f"{shown(one)} cannot run after itself.")
        if n in seen or n not in every:
            return
        for a in every[n].after:
            walk(a, path + (n,))
        seen.add(n)
    walk(one.name, ())


def depth(schedules: dict[str, Schedule], name: str, _seen=()) -> int:
    """How many follows deep a schedule is: 0 for one that follows none."""
    s = schedules.get(name)
    if s is None or name in _seen:
        return 0
    return max((1 + depth(schedules, a, _seen + (name,)) for a in s.after), default=0)


def read_reviewer(folder: Path, reviewer: str) -> str | None:
    p = folder / reviewer
    return p.read_text() if reviewer and p.is_file() else None


def load(folder: Path, problems: list, substitute=lambda text: text) -> dict[str, Schedule]:
    """Every valid schedule in `folder`, by name, its `title` the label with its name
    and the user's filled in (`substitute(text)`; the label stays as written, so a save
    keeps its placeholders). Each bad file is one Problem."""
    from ..config import Problem, shown_path
    from ..errors import CelineError
    out: dict[str, Schedule] = {}
    for path in sorted(folder.glob("*.toml")):
        try:
            raw = tomllib.loads(path.read_text())
            s = parse(path.stem, raw, read_reviewer(folder, str(raw.get("reviewer") or "")))
            out[path.stem] = replace(s, title=substitute(s.label))
        except (tomllib.TOMLDecodeError, OSError) as e:
            problems.append(Problem(shown_path(path), f"not loaded: {e}"))
        except CelineError as e:
            problems.append(Problem(shown_path(path), f"{e.message} The schedule is not loaded."))
    for s in out.values():
        for a in s.after:
            if a not in out:
                problems.append(Problem(shown_path(folder / f"{s.name}.toml"),
                                        f"runs after {a}, which is not a schedule."))
    return out


def night_of(at: datetime, tz) -> date:
    """The night a moment belongs to: before 12:00 in the app's zone that date's night,
    from 12:00 on the next date's. A run's night, Run now's, the page's tonight."""
    local = at.astimezone(tz)
    return local.date() + timedelta(days=local.hour >= 12)


def run_date(s: Schedule, slot: datetime, tz) -> date:
    """The date a run of `s` at `slot` fills in: the calendar date of the slot for a
    daytime job (Radar at 17:00 writes under today), else its night."""
    return slot.astimezone(tz).date() if s.daytime else night_of(slot, tz)


def night_start(night: date, tz) -> datetime:
    """The first moment of `night`: 12:00 on the date before."""
    return datetime.combine(night - timedelta(days=1), time(12), tz)


def fill(cfg, text: str, day: date, results: dict[str, str],
         extra: dict[str, str] | None = None) -> str:
    """`text` with the dates of `day` (the run's date, run_date), the results of the
    same night (NONE for a
    schedule without one), `extra` ({{mood}} and the others of schedules/variables.py,
    each where given) and its name, pronouns and {{username}}."""
    for key, value in (extra or {}).items():
        text = text.replace("{{" + key + "}}", value)
    text = RESULT.sub(lambda m: results.get(m[1]) or NONE, text)
    text = (text.replace("{{date}}", day.isoformat())
            .replace("{{day_before}}", (day - timedelta(days=1)).isoformat())
            .replace("{{weekday}}", day.strftime("%A")))
    return cfg.substitute(text)
