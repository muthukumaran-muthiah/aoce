"""Alertmanager webhook payload (version 4). See AOCE-21."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Alert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    generator_url: str | None = Field(default=None, alias="generatorURL")
    fingerprint: str | None = None

    @property
    def service(self) -> str | None:
        return self.labels.get("service_name") or self.labels.get("service") or self.labels.get("job")


class AlertmanagerWebhook(BaseModel):
    version: str = "4"
    group_key: str = Field(alias="groupKey")
    status: Literal["firing", "resolved"]
    receiver: str
    group_labels: dict[str, str] = Field(default_factory=dict, alias="groupLabels")
    common_labels: dict[str, str] = Field(default_factory=dict, alias="commonLabels")
    common_annotations: dict[str, str] = Field(default_factory=dict, alias="commonAnnotations")
    external_url: str | None = Field(default=None, alias="externalURL")
    alerts: list[Alert]
