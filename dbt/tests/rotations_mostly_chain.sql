-- Planes fly connected chains, so in a normal month at least 95% of legs should link cleanly to
-- the previous one. A drop means bad tail numbers or a broken join, not a change in aviation.
select year, month, count(*) as legs, avg(chain_ok::integer) as chain_ok_share
from {{ ref('fct_rotations') }}
group by all
having avg(chain_ok::integer) < 0.95
