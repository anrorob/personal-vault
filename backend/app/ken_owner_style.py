"""Structured owner preferences only; no descriptions or cross-asset facts."""
from uuid import UUID
from psycopg.types.json import Jsonb
from app import ken_style as style


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS vault_ken_preferences (
        owner_user_id UUID PRIMARY KEY, preferences JSONB NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


def owner_lock(c,owner):
    if not isinstance(owner,UUID):raise ValueError('Immutable owner UUID required')
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,761017))',(str(owner),))


def save_preferences(c,owner,value,*,seed=False):
    owner_lock(c,owner); value=style.validate_preferences(value)
    if seed:
        c.execute('INSERT INTO vault_ken_preferences(owner_user_id,preferences) VALUES(%s,%s) ON CONFLICT DO NOTHING',(owner,Jsonb(value)))
    else:
        c.execute('''INSERT INTO vault_ken_preferences(owner_user_id,preferences) VALUES(%s,%s)
            ON CONFLICT(owner_user_id) DO UPDATE SET preferences=EXCLUDED.preferences,
            revision=vault_ken_preferences.revision+1,updated_at=CURRENT_TIMESTAMP''',(owner,Jsonb(value)))


def preferences(c,owner):
    if not isinstance(owner,UUID):raise ValueError('Immutable owner UUID required')
    row=c.execute('SELECT preferences,revision FROM vault_ken_preferences WHERE owner_user_id=%s',(owner,)).fetchone()
    return row or {'preferences':{},'revision':0}


def context(c,owner):
    p=preferences(c,owner)
    return {'version':style.VERSION,'preference_revision':p['revision'],'preferences':style.validate_preferences(p['preferences'])}


def seed_owner_profile(owner):
    """Explicit Development operation, never an automatic per-user default."""
    import os
    from app.ken_service import get_ken_store
    if os.environ.get('PV_ENVIRONMENT')!='development':raise ValueError('Development only')
    with get_ken_store().connect() as c:
        if not c.execute('SELECT user_id FROM auth_accounts WHERE user_id=%s',(owner,)).fetchone():
            raise ValueError('Owner account not found')
        save_preferences(c,owner,style.DEFAULTS,seed=True)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--seed-owner',required=True,type=UUID)
    args=p.parse_args();seed_owner_profile(args.seed_owner)
    print('Owner profile seeded idempotently; existing preferences preserved')
