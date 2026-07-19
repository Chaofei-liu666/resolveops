# ResolveOps schema migrations

`0001_webhook_idempotency.sql` prevents duplicate ERP webhook delivery from
creating duplicate Cases. The application currently executes this idempotently
at startup to upgrade the running pilot. Before a production rollout, apply
these files through CI using a database migration user, then remove DDL rights
from the API and Worker roles.
