"""widget_bot_by_public_id: the one cross-tenant read the widget needs.

The widget loader starts from a public bot id, which by definition has no tenant
context yet (§12.3). Under the §16.3 policy a connection with no `app.tenant_id`
sees zero rows, so this lookup cannot be done from an ordinary scoped session.

Three options were available:

1. **Connect as the table owner for this one query.** Rejected: it would put owner
   credentials in the API process, and one privileged code path is all it takes for
   an injection bug to become a full-tenant read.
2. **Add a permissive SELECT policy on `bots`.** Rejected: it would make every bot
   row readable by any code path that forgets to set a tenant, which is exactly the
   mistake RLS exists to catch.
3. **A `SECURITY DEFINER` function with a single, fixed query and a frozen
   `search_path`.** Chosen. The blast radius is one statement that returns
   presentation data for one public id — no tenant documents, no other tables.

`search_path` is pinned because an unpinned one lets a caller who can create objects
shadow `bots` and run arbitrary SQL with the definer's rights. That is the classic
`SECURITY DEFINER` escalation, and it costs one line to prevent.

The returned columns are deliberately the widget's whole world: name, welcome copy,
appearance, suggestion settings, the powered-by flag, and the bot's own tenant id so
the caller can open a scoped session. Nothing about the tenant itself is exposed.

Revision ID: 0006_widget_public_lookup
Revises: 0005_version_payload
"""

from alembic import op

revision = "0006_widget_public_lookup"
down_revision = "0005_version_payload"
branch_labels = None
depends_on = None

APP_ROLE = "eva_app"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION widget_bot_by_public_id(p_public_id text)
        RETURNS TABLE (
            id uuid,
            tenant_id uuid,
            name text,
            welcome_message text,
            appearance jsonb,
            suggestion_settings jsonb,
            powered_by_visible boolean
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT b.id, b.tenant_id, b.name, b.welcome_message, b.appearance,
                   b.suggestion_settings, b.powered_by_visible
              FROM public.bots b
             WHERE b.public_id = p_public_id
             LIMIT 1
        $$;
        """
    )
    # Only this role may call it, and it may not be swapped out from under the app.
    op.execute(f"REVOKE ALL ON FUNCTION widget_bot_by_public_id(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION widget_bot_by_public_id(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS widget_bot_by_public_id(text)")
