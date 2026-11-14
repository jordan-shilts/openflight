"""Upload shots to the owner's own Supabase project.

The database side is ``scripts/supabase/schema.sql``: a Pi writes through the
project's REST API with the publishable key plus its own device token, and the
database ties every row to the device that token belongs to.
"""
