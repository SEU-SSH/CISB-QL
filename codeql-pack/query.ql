import cpp
import query

from Function f
where matches(f)
select f, f.getName()
