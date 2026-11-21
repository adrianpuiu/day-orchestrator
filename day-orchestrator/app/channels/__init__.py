"""Channel adapters (lowest module layer above core).

Import discipline (ADR-006, CI-enforced): channels import only stdlib, their
third-party client, and `app.core.*`; never each other, never workflows/tools.
gmail.py owns smtplib+imapclient; telegram.py owns aiogram; calendar.py owns
the Google API client libs.
"""
