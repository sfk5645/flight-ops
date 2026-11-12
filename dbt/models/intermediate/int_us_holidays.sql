-- US federal holidays, computed from their rules so no calendar file needs updating. Holidays
-- that fall on a weekend are kept on their actual date: travel peaks around the day itself.
with years as (
    select unnest(range(2020, 2041)) as y
),

rules (name, month, weekday, nth, fixed_day) as (
    -- weekday: ISO (1 = Monday); nth: which occurrence in the month, -1 = last
    values
        ('New Year''s Day', 1, null, null, 1),
        ('Martin Luther King Jr. Day', 1, 1, 3, null),
        ('Presidents'' Day', 2, 1, 3, null),
        ('Memorial Day', 5, 1, -1, null),
        ('Juneteenth', 6, null, null, 19),
        ('Independence Day', 7, null, null, 4),
        ('Labor Day', 9, 1, 1, null),
        ('Columbus Day', 10, 1, 2, null),
        ('Veterans Day', 11, null, null, 11),
        ('Thanksgiving', 11, 4, 4, null),
        ('Christmas Day', 12, null, null, 25)
),

months as (
    select
        r.name,
        r.weekday,
        r.nth,
        r.fixed_day,
        make_date(y.y::integer, r.month, 1) as first_day,
        last_day(make_date(y.y::integer, r.month, 1)) as last_day
    from years as y
    cross join rules as r
)

select
    name,
    case
        when fixed_day is not null then first_day + (fixed_day - 1)::integer
        when nth > 0
            then first_day + (((weekday - isodow(first_day) + 7) % 7) + 7 * (nth - 1))::integer
        else last_day - ((isodow(last_day) - weekday + 7) % 7)::integer
    end as holiday_date
from months
