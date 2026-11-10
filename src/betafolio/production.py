"""Global operator configuration; private preferences belong to accounts."""
from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .config import Settings


class DailySchedule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    at: time = time(17)
    timezone: str = 'America/New_York'

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (KeyError, ValueError):
            raise ValueError('Choose a valid IANA timezone, such as America/New_York') from None
        return value

    @field_validator('at')
    @classmethod
    def local_time(cls, value):
        if value.tzinfo or value.second or value.microsecond:
            raise ValueError('Choose a local hour and minute')
        return value


class EmailSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    transport: Literal['resend', 'smtp'] = 'resend'
    sender: str = ''
    sender_name: str = 'Betafolio'
    smtp_host: str = ''
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str = ''
    smtp_tls: Literal['implicit', 'starttls'] = 'implicit'

    @field_validator('sender_name', 'smtp_host', 'smtp_username')
    @classmethod
    def no_headers(cls, value):
        if any(c in value for c in '\r\n\x00') or len(value) > 254:
            raise ValueError('Invalid email setting')
        return value

    @field_validator('sender')
    @classmethod
    def address(cls, value):
        from email_validator import validate_email
        return validate_email(value, check_deliverability=False).normalized if value else ''


class ProductionSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    research: Settings = Field(default_factory=Settings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    schedule: DailySchedule = Field(default_factory=DailySchedule)
    daily_allowance: int = Field(default=3, ge=0, le=100)
    concurrency: int = Field(default=2, ge=1, le=8)
    signup_enabled: bool = False
    input_cost_per_million: float | None = Field(default=None, ge=0, le=1000)
    output_cost_per_million: float | None = Field(default=None, ge=0, le=1000)


def settings(database):
    return ProductionSettings.model_validate(database.get('settings', {}))


def ready(database, *, kinds=('model', 'search')):
    from .connections import revision
    from .service import ConnectionService
    service = ConnectionService(database)
    checks = database.get('readiness', {})
    return all(checks.get(kind, {}).get('revision') == revision(service, kind)
               and checks[kind].get('ok') for kind in kinds)


def email_ready(database):
    return ready(database, kinds=('email',))
